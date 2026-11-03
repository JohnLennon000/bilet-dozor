"""Ссылки «купить»: в шаблонах config.yaml подставляются названия, коды и дата.

Подстановки: {date} 2027-01-09, {dd} {mm} {yyyy}; для поезда {from_code} {to_code} {from_slug} {to_slug}
{From_slug} {To_slug} (латиницей, первая буква заглавная); для рейсов {origin} {destination}.
Неизвестная подстановка оставляет шаблон как есть — ссылку с такой опечаткой лучше не показывать вовсе.
"""
from __future__ import annotations

import re

from dozor.util import slug

_FIELD = re.compile(r"\{(\w+)\}")


def _fill(template: str, values: dict) -> str | None:
    missing = False

    def sub(m: re.Match) -> str:
        nonlocal missing
        v = values.get(m.group(1))
        if v is None or v == "":
            missing = True
            return m.group(0)
        return str(v)

    out = _FIELD.sub(sub, template)
    return None if missing else out


def _date_parts(date: str) -> dict:
    y, m, d = date.split("-")
    return {"date": date, "dd": d, "mm": m, "yyyy": y}


def train_links(cfg, route: dict | None, date: str) -> list[dict]:
    t = cfg.train
    f, to = slug(t.from_name), slug(t.to_name)
    values = {
        **_date_parts(date),
        "from_code": ((route or {}).get("from") or {}).get("code") or t.from_code,
        "to_code": ((route or {}).get("to") or {}).get("code") or t.to_code,
        "from_slug": f, "to_slug": to, "From_slug": f.capitalize(), "To_slug": to.capitalize(),
    }
    out = []
    for link in t.links:
        url = _fill(link["url"], values)
        if url:
            out.append({"label": link["label"], "url": url, "primary": link.get("primary", False)})
        elif link.get("primary"):  # нет кодов станций — хотя бы открываем главную страницу РЖД
            out.append({"label": "Открыть РЖД", "url": "https://ticket.rzd.ru/", "primary": True})
    return out


def flight_links(cfg, date: str) -> list[dict]:
    values = {**_date_parts(date), "origin": cfg.flights.origin, "destination": cfg.flights.destination}
    out = []
    for link in cfg.flights.links:
        url = _fill(link["url"], values)
        if url:
            out.append({"label": link["label"], "url": url, "primary": link.get("primary", False)})
    return out
