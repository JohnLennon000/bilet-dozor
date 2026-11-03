"""Пуш-уведомления в установленное приложение.

Подписки телефонов лежат в папке push/ репозитория: приложение открывает GitHub с готовым файлом,
владелец нажимает «Commit changes». Ключ VAPID выводится из секрета PUSH_SECRET (или TRAVELPAYOUTS_TOKEN),
нигде не хранится и недоступен никому, кроме запуска в Actions.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from dozor import webpush

log = logging.getLogger(__name__)

TTL_URGENT = 12 * 3600  # если телефон без сети, срочное уведомление подождёт до 12 часов
TTL_NORMAL = 3 * 3600


def contact_claim(contact: str | None) -> str:
    """Поле sub в подписи VAPID: mailto: или адрес сайта без пути."""
    if contact and contact.startswith("mailto:"):
        return contact
    if contact and contact.startswith("https://"):
        return f"https://{urlsplit(contact).netloc}"
    return "https://github.com"


@dataclass
class Device:
    id: str
    path: Path
    info: dict


class Push:
    def __init__(self, seed: str | None, root: Path, contact: str | None, *, dry_run: bool = False, session=None, clock=time.time, allow_insecure: bool = False) -> None:
        self.directory = Path(root) / "push"
        self.contact = contact_claim(contact)
        self.dry_run = dry_run
        self.allow_insecure = allow_insecure  # только для тестов: разрешает http://127.0.0.1
        self.session = session or requests
        self.clock = clock
        self.outbox: list[dict] = []
        self._key = webpush.derive_key(seed) if seed else None
        self.public_key = webpush.public_key_b64(self._key) if self._key else None

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def devices(self) -> list[Device]:
        if not self.enabled or not self.directory.is_dir():
            return []
        found: dict[str, Device] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("Подписка %s не читается: %s", path.name, type(exc).__name__)
                continue
            err = webpush.validate_subscription(info, allow_insecure=self.allow_insecure)
            if err:
                log.warning("Подписка %s не подходит: %s", path.name, err)
                continue
            keys = info["keys"]
            clean = {"endpoint": info["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
            dev = Device(webpush.device_id(info["endpoint"]), path, clean)
            found.setdefault(dev.id, dev)
        return list(found.values())

    def send(self, device: Device, payload: dict, *, urgent: bool = False) -> str:
        """Возвращает "ok", "gone" (подписки больше нет) или "error"."""
        if self.dry_run:
            self.outbox.append({"device": device.id, **payload})
            return "ok"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            data = webpush.encrypt_payload(body, device.info["keys"])
            headers = {
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL_URGENT if urgent else TTL_NORMAL),
                "Urgency": "high" if urgent else "normal",
                "Authorization": webpush.vapid_authorization(device.info["endpoint"], self._key, self.contact, int(self.clock())),
            }
            resp = self.session.post(device.info["endpoint"], data=data, headers=headers, timeout=20)
        except ValueError as exc:
            log.warning("Пуш на %s не отправлен: %s", device.id, exc)
            return "error"
        except requests.RequestException as exc:
            log.warning("Пуш на %s не ушёл: %s", device.id, type(exc).__name__)
            return "error"
        if 200 <= resp.status_code < 300:
            return "ok"
        if resp.status_code in (404, 410):
            return "gone"
        log.warning("Пуш на %s не ушёл: HTTP %s %s", device.id, resp.status_code, resp.text[:80])
        return "error"
