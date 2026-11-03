"""HTTP-помощник для источников: единый формат ошибок и защита от «HTML вместо JSON»."""
from __future__ import annotations

import re
import time

import requests

from dozor.util import SourceError

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}


def snippet(text: str, n: int = 160) -> str:
    return re.sub(r"\s+", " ", str(text))[:n]


def get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 20,
    session: requests.Session | None = None,
    proxy: str | None = None,
    retries: int = 1,
) -> tuple[object, dict, str]:
    """GET → (json, meta, text). Бросает SourceError с понятным текстом на русском."""
    http = session or requests
    proxies = {"https": proxy, "http": proxy} if proxy else None
    started = time.monotonic()
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = http.get(url, params=params, headers=headers, timeout=timeout, proxies=proxies)
            break
        except requests.RequestException as exc:
            if attempt < retries:
                time.sleep(3)
                continue
            why = "таймаут" if isinstance(exc, requests.Timeout) else type(exc).__name__
            raise SourceError(f"Нет связи с источником ({why})", "network") from None
    assert resp is not None
    text = resp.text
    shown_url = re.sub(r"(token=)[^&]+", r"\1***", resp.url, flags=re.I)
    meta = {"url": shown_url, "status": resp.status_code, "ms": int((time.monotonic() - started) * 1000), "bytes": len(text)}
    code = resp.status_code
    if code == 429:
        raise SourceError("Источник просит не частить (HTTP 429)", "rate", meta)
    if code == 401:
        raise SourceError("Источник отклонил ключ доступа (HTTP 401)", "auth", meta)
    if code == 403:
        raise SourceError("Источник закрыл доступ (HTTP 403) — часто это блокировка по IP или защита от ботов", "blocked", meta)
    if code >= 500:
        raise SourceError(f"Ошибка на стороне источника (HTTP {code})", "server", meta)
    if code >= 400:
        raise SourceError(f"Неожиданный ответ источника (HTTP {code}): {snippet(text, 80)}", "http", meta)
    try:
        payload = resp.json()
    except ValueError:
        raise SourceError(f"Источник вернул не JSON (похоже на страницу-заглушку): {snippet(text, 100)}", "blocked", meta) from None
    return payload, meta, text
