import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from dozor import webpush
from dozor.webpush import b64u, unb64u
from tests.helpers import PushService, decrypt_aes128gcm


def test_encryption_matches_rfc8291_appendix_a_bytewise():
    as_private = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
    ua_public = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    auth = "BTBZMqHH6r4Tts7J_aSIgg"
    salt = "DGv6ra1nlYgDCS1FRnbzlw"
    expected = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
                "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    sender = ec.derive_private_key(int.from_bytes(unb64u(as_private), "big"), ec.SECP256R1())
    out = webpush.encrypt_payload(b"When I grow up, I want to be a watermelon", {"p256dh": ua_public, "auth": auth}, salt=unb64u(salt), sender_key=sender)
    assert b64u(out) == expected


def test_recipient_can_decrypt_cyrillic_and_long_text():
    ua = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    keys = {"p256dh": b64u(pub), "auth": b64u(auth)}
    for text in ("x", "Появились билеты ✓", "Купе — 4 места от 4 200 ₽. " * 70):
        enc = webpush.encrypt_payload(text.encode(), keys)
        assert decrypt_aes128gcm(enc, ua, auth).decode() == text


def test_too_big_and_broken_keys_rejected():
    svc = PushService()
    keys = svc.subscription()["keys"]
    svc.close()
    with pytest.raises(ValueError, match="слишком большое"):
        webpush.encrypt_payload(b"x" * 4000, keys)
    with pytest.raises(ValueError, match="p256dh"):
        webpush.encrypt_payload(b"x", {**keys, "p256dh": "AAAA"})
    with pytest.raises(ValueError, match="auth"):
        webpush.encrypt_payload(b"x", {**keys, "auth": "AA"})


def test_vapid_jwt_is_valid_es256():
    key = webpush.derive_key("секрет")
    header = webpush.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", key, "https://example.org", now=1_800_000_000)
    assert header.startswith("vapid t=")
    jwt, k = header[len("vapid t="):].split(", k=")
    assert k == webpush.public_key_b64(key)
    head, claims, sig = jwt.split(".")
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    c = json.loads(unb64u(claims))
    assert c == {"aud": "https://fcm.googleapis.com", "exp": 1_800_000_000 + 12 * 3600, "sub": "https://example.org"}
    raw = unb64u(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # бросит исключение, если подпись неверна


def test_key_derivation_is_stable_and_seed_dependent():
    a, b = webpush.derive_key("one"), webpush.derive_key("one")
    assert webpush.public_key_b64(a) == webpush.public_key_b64(b)
    assert webpush.public_key_b64(a) != webpush.public_key_b64(webpush.derive_key("two"))
    assert len(unb64u(webpush.public_key_b64(a))) == 65


def test_endpoint_validation_blocks_ssrf():
    assert webpush.validate_endpoint("https://fcm.googleapis.com/fcm/send/x") is None
    assert webpush.validate_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x") is None
    for bad in ("http://fcm.googleapis.com/x", "https://localhost/x", "https://127.0.0.1/x", "https://[::1]/x", "https://10.0.0.5/x", "https://printer.local/x", "ftp://x", "не url", 123):
        assert webpush.validate_endpoint(bad) is not None, bad
    assert webpush.validate_endpoint("http://127.0.0.1:5000/x", allow_insecure=True) is None


def test_device_id_matches_browser_algorithm():
    # браузер считает SHA-256(endpoint) в hex и берёт 12 символов; имя файла в push/ должно совпасть
    import hashlib
    ep = "https://fcm.googleapis.com/fcm/send/abc"
    assert webpush.device_id(ep) == hashlib.sha256(ep.encode()).hexdigest()[:12]
