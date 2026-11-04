"""Синтетические ответы источников для тестов.

ВАЖНО: это НЕ записи настоящих ответов РЖД и Aviasales (из среды разработки они недоступны). Данные собраны по
описанию протокола в открытом клиенте github.com/drGOD/rzd-api и по документации Travelpayouts. Настоящий ответ
положите в tests/fixtures и напишите на нём тест разбора (см. tests/fixtures/README.md).
"""
from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from dozor.config import parse_config
from dozor.webpush import b64u, unb64u

CAR_NAMES = {"Compartment": "Купе", "ReservedSeat": "Плацкарт", "Soft": "СВ", "Sedentary": "Сидячий"}


def group(car_type: str, seats: int | None, min_price: int | None, **extra) -> dict:
    g = {"CarType": car_type, "CarTypeName": CAR_NAMES.get(car_type, car_type), "MinPrice": min_price,
         "MaxPrice": None if min_price is None else round(min_price * 1.4), "TotalPlaceQuantity": seats}
    g.update(extra)
    return g


def train(number: str = "301У", groups: list | None = (), **extra) -> dict:
    t = {
        "TrainNumber": number, "DisplayTrainNumber": number, "OriginStationName": "ТУЙМАЗЫ", "DestinationStationName": "МОСКВА КАЗАНСКАЯ",
        "DepartureDateTime": "2027-01-09T05:47:00", "LocalDepartureDateTime": "2027-01-09T07:47:00",
        "ArrivalDateTime": "2027-01-10T05:10:00", "LocalArrivalDateTime": "2027-01-10T05:10:00",
    }
    if groups is not None:
        t["CarGroups"] = list(groups)
    t.update(extra)
    return t


def rzd_response(trains: list, **extra) -> dict:
    return {"Trains": trains, "MoscowDateTime": "2026-11-03T12:00:00", **extra}


def offer(flight_number: str = "101", price: int = 6190, **extra) -> dict:
    o = {
        "origin": "UUA", "destination": "MOW", "origin_airport": "UUA", "destination_airport": "VKO", "price": price,
        "airline": "RT", "flight_number": flight_number, "departure_at": "2027-01-09T10:20:00+03:00", "transfers": 0,
        "duration_to": 125, "link": f"/search/UUA0901MOW1?t=RT{flight_number}",
    }
    o.update(extra)
    return o


def tp_response(offers: list, **extra) -> dict:
    return {"success": True, "data": offers, "currency": "rub", **extra}


def make_cfg(**over):
    raw = {
        "train": {"dates": ["2027-01-09"], "from_code": "2024750", "to_code": "2000000"},
        "flights": {"dates": ["2027-01-09", "2027-01-10"]},
        "notify": {"price_gap_min": 0},
    }
    for k, v in over.items():
        raw.setdefault(k, {}).update(v)
    return parse_config(raw)


def write_fixture(directory: Path, name: str, payload: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ───────────────────────── поддельный push-сервис ─────────────────────────

def decrypt_aes128gcm(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """Расшифровка как в браузере (RFC 8291) — проверяет, что получатель сможет прочитать сообщение."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    ua_public = ua_private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert rs == 4096
    return plain.rstrip(b"\x00")[:-1] if plain.endswith(b"\x02") else plain


class PushService:
    """Принимает POST как push-служба браузера, расшифровывает и складывает в .messages."""

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.messages: list[dict] = []
        self.ua_private = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if outer.status < 300:
                    text = decrypt_aes128gcm(body, outer.ua_private, outer.auth)
                    outer.messages.append({"payload": json.loads(text), "headers": dict(self.headers)})
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/push/abc"

    def subscription(self) -> dict:
        pub = self.ua_private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "keys": {"p256dh": b64u(pub), "auth": b64u(self.auth)}}

    def close(self) -> None:
        self.server.shutdown()
