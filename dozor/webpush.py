"""Web Push без pywebpush: VAPID (RFC 8292) + шифрование aes128gcm (RFC 8188, RFC 8291).

Нужна только библиотека cryptography. Закрытый ключ VAPID нигде не хранится: он каждый раз выводится
из секрета репозитория (PUSH_SECRET), поэтому отправить уведомление по подписке из папки push/ может
только этот сервис.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
RECORD_SIZE = 4096
MAX_PAYLOAD = 3500  # push-службы принимают до 4096 байт вместе с заголовком шифрования


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_key(seed: str) -> ec.EllipticCurvePrivateKey:
    digest = hmac.new(seed.encode(), b"bilet-dozor/vapid/p256", hashlib.sha256).digest()
    scalar = int.from_bytes(digest, "big") % (P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(raw)


def device_id(endpoint: str) -> str:
    """Те же 12 символов, что считает приложение в браузере: SHA-256(endpoint), hex."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def vapid_authorization(endpoint: str, key: ec.EllipticCurvePrivateKey, subject: str, now: int, ttl: int = 12 * 3600) -> str:
    parts = urlsplit(endpoint)
    aud = f"{parts.scheme}://{parts.netloc}"
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": now + ttl, "sub": subject}, separators=(",", ":")).encode())
    signing_input = f"{head}.{claims}"
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # JWS: «сырая» подпись r||s, а не DER
    return f"vapid t={signing_input}.{b64u(signature)}, k={public_key_b64(key)}"


def encrypt_payload(
    plaintext: bytes,
    keys: dict,
    *,
    salt: bytes | None = None,
    sender_key: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """Шифрует сообщение для подписки браузера (aes128gcm). salt и sender_key задают только тесты."""
    ua_public = unb64u(keys["p256dh"])
    auth_secret = unb64u(keys["auth"])
    if len(ua_public) != 65 or ua_public[0] != 4:
        raise ValueError("Подписка: некорректный ключ p256dh")
    if len(auth_secret) < 16:
        raise ValueError("Подписка: некорректный секрет auth")
    if len(plaintext) > MAX_PAYLOAD:
        raise ValueError(f"Сообщение слишком большое ({len(plaintext)} байт)")

    sender = sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = sender.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), ua_key)
    salt = salt or os.urandom(16)

    # RFC 8291, раздел 3.4
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)

    encrypted = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # одна (последняя) запись, разделитель 0x02
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + encrypted


def validate_endpoint(endpoint: object, *, allow_insecure: bool = False) -> str | None:
    """Возвращает текст ошибки или None. Защита от SSRF: только https и только имена хостов, не IP."""
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return "некорректный endpoint"
    parts = urlsplit(endpoint)
    if not parts.scheme or not parts.hostname:
        return "некорректный endpoint"
    if allow_insecure:
        return None
    if parts.scheme != "https":
        return "endpoint должен быть https"
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")) or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return "недопустимый хост endpoint"
    return None


def validate_subscription(sub: object, *, allow_insecure: bool = False) -> str | None:
    if not isinstance(sub, dict):
        return "нет подписки"
    err = validate_endpoint(sub.get("endpoint"), allow_insecure=allow_insecure)
    if err:
        return err
    keys = sub.get("keys")
    if not isinstance(keys, dict):
        return "нет ключей"
    try:
        p256dh, auth = unb64u(keys.get("p256dh", "")), unb64u(keys.get("auth", ""))
    except (ValueError, TypeError):
        return "ключи не base64"
    if len(p256dh) != 65 or p256dh[0] != 4:
        return "некорректный ключ p256dh"
    if len(auth) < 16:
        return "некорректный секрет auth"
    return None
