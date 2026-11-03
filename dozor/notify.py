"""Доставка уведомлений: пуш в приложение (основной канал) и Telegram (необязательный)."""
from __future__ import annotations

import html
import logging
from urllib.parse import quote

from dozor.config import Notify
from dozor.push import Push
from dozor.telegram import Telegram, seal, unseal
from dozor.util import msk

log = logging.getLogger(__name__)

TITLE_MAX = 140
BODY_MAX = 600


def watch_url(watch: str) -> str:
    """Относительный адрес карточки в приложении; service worker сам превращает его в полный."""
    return f"./#w={quote(watch, safe=':')}"


def push_payload(ev: dict, links: list[dict], *, silent: bool) -> dict:
    return {
        "v": 1,
        "kind": ev["kind"],
        "title": ev["title"][:TITLE_MAX],
        "body": ev["body"][:BODY_MAX],
        "tag": ev.get("tag") or ev["kind"],
        "urgent": bool(ev.get("urgent")),
        "silent": silent,
        "url": watch_url(ev["watch"]),
        "links": [{"label": l["label"], "url": l["url"]} for l in links[:2]],
    }


def telegram_html(ev: dict, links: list[dict], site_url: str | None) -> str:
    parts = [f"<b>{html.escape(ev['title'], quote=False)}</b>", html.escape(ev["body"], quote=False)]
    anchors = [f'<a href="{html.escape(l["url"], quote=True)}">{html.escape(l["label"], quote=False)}</a>' for l in links]
    if site_url:
        anchors.append(f'<a href="{html.escape(site_url, quote=True)}">Дозор</a>')
    if anchors:
        parts.append(" · ".join(anchors))
    return "\n".join(parts)


class Notifier:
    def __init__(self, push: Push, tg: Telegram, state: dict, notify: Notify, site_url: str | None) -> None:
        self.push = push
        self.tg = tg
        self.state = state
        self.notify = notify
        self.site_url = site_url
        self._tg_ready: bool | None = None

    # --- Telegram ------------------------------------------------------------------------
    def telegram_ready(self) -> bool:
        if self._tg_ready is None:
            self._tg_ready = self._connect_telegram()
        return self._tg_ready

    def _connect_telegram(self) -> bool:
        if not self.tg.configured:
            return False
        if self.tg.chat_id:
            return True
        tstate = self.state.setdefault("telegram", {})
        sealed = tstate.get("chat")
        if sealed and not self.tg.dry_run:
            self.tg.chat_id = unseal(sealed, self.tg.token)
            if self.tg.chat_id:
                return True
        chat = self.tg.discover_chat()
        if not chat:
            log.warning("Telegram: бот ещё не знает, кому писать — откройте его и нажмите «Запустить» (/start)")
            return False
        self.tg.chat_id = chat
        if not self.tg.dry_run:
            tstate["chat"] = seal(chat, self.tg.token)
        return True

    # --- рассылка ------------------------------------------------------------------------
    def silent_now(self, ev: dict, now: int) -> bool:
        return not ev.get("urgent") and self.notify.is_quiet(msk(now).hour)

    def send(self, ev: dict, links: list[dict], now: int) -> dict:
        """Отправляет событие по всем каналам. Возвращает {push: {ok, failed}, telegram: bool|None}."""
        silent = self.silent_now(ev, now)
        result = {"push": {"ok": 0, "failed": 0}, "telegram": None}
        if self.push.enabled:
            known = set(self.state.setdefault("push", {}).get("known", []))
            for dev in self.push.devices():
                if dev.id not in known:
                    continue  # новому телефону сначала уходит приветствие (см. greet_new_devices)
                status = self.push.send(dev, push_payload(ev, links, silent=silent), urgent=bool(ev.get("urgent")))
                if status == "ok":
                    result["push"]["ok"] += 1
                else:
                    result["push"]["failed"] += 1
                if status == "gone":
                    log.info("Подписка %s больше не действует — удаляю %s", dev.id, dev.path.name)
                    dev.path.unlink(missing_ok=True)
                    known.discard(dev.id)
            self.state["push"]["known"] = sorted(known)
        if self.telegram_ready():
            result["telegram"] = self.tg.send_message(telegram_html(ev, links, self.site_url), silent=silent)
        return result

    def greet_new_devices(self, body: str, now: int) -> int:
        """Подключает новые телефоны из push/: пробное уведомление со сводкой, после него устройство считается известным."""
        if not self.push.enabled:
            return 0
        pstate = self.state.setdefault("push", {})
        known = set(pstate.get("known", []))
        added = 0
        for dev in self.push.devices():
            if dev.id in known:
                continue
            ev = {"kind": "welcome", "title": "✅ Уведомления подключены", "body": body, "tag": "welcome", "urgent": False, "watch": "train"}
            payload = push_payload(ev, [], silent=False)
            payload["url"] = "./"
            status = self.push.send(dev, payload)
            if status == "ok":
                known.add(dev.id)
                added += 1
                log.info("Подключён телефон %s", dev.id)
            elif status == "gone":
                dev.path.unlink(missing_ok=True)
        pstate["known"] = sorted(known)
        return added

    def prune_known(self) -> None:
        pstate = self.state.setdefault("push", {})
        existing = {d.id for d in self.push.devices()}
        pstate["known"] = sorted(set(pstate.get("known", [])) & existing)
