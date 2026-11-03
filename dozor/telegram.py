"""Telegram: необязательный дополнительный канал. Работает, если задан секрет TELEGRAM_BOT_TOKEN."""
from __future__ import annotations

import base64
import hashlib
import json
import logging

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000


def seal(chat_id: str, token: str) -> str:
    """chat_id хранится в публичном репозитории только в зашифрованном виде: ключ выводится из токена бота."""
    key = hashlib.sha256(("chat:" + token).encode()).digest()
    return base64.urlsafe_b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(chat_id.encode()))).decode()


def unseal(blob: str, token: str) -> str | None:
    try:
        data = base64.urlsafe_b64decode(blob.encode())
    except (ValueError, TypeError):
        return None
    key = hashlib.sha256(("chat:" + token).encode()).digest()
    text = bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode(errors="replace")
    return text if text.lstrip("-").isdigit() else None


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None = None, *, dry_run: bool = False, session=None) -> None:
        self.token = token or ""
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.session = session or requests
        self.outbox: list[dict] = []

    @property
    def configured(self) -> bool:
        return self.dry_run or bool(self.token)

    def _call(self, method: str, data: dict) -> object | None:
        url = API.format(token=self.token, method=method)
        try:
            resp = self.session.post(url, data=data, timeout=30)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Telegram %s: %s", method, str(exc).replace(self.token, "***"))
            return None
        if not payload.get("ok"):
            log.warning("Telegram %s: %s", method, payload.get("description"))
            return None
        return payload.get("result")

    def discover_chat(self) -> str | None:
        """Ищет личный чат, в котором боту написали (например, /start)."""
        if self.dry_run:
            return "dry-run"
        updates = self._call("getUpdates", {"timeout": 0, "allowed_updates": json.dumps(["message"])})
        for update in updates or []:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id"):
                return str(chat["id"])
        return None

    def send_message(self, html: str, *, silent: bool = False) -> bool:
        if self.dry_run:
            self.outbox.append({"text": html, "silent": silent})
            return True
        ok = True
        for chunk in split_message(html):
            result = self._call("sendMessage", {
                "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                "link_preview_options": json.dumps({"is_disabled": True}), "disable_notification": json.dumps(silent),
            })
            ok = ok and result is not None
        return ok
