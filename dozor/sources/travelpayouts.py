"""Источник авиабилетов: Aviasales Data API (Travelpayouts), эндпоинт prices_for_dates.

ВАЖНО: это кэш поисков пользователей Aviasales за последние дни, а не «живая» выдача авиакомпании.
Отсутствие рейса в ответе НЕ означает, что билетов нет, а его появление — надёжный сигнал, что билеты в продаже.
Поэтому монитор держит найденное предложение в списке ещё 6 часов после исчезновения из кэша.
"""
from __future__ import annotations

from dozor.http import get_json
from dozor.util import SourceError

NAME = "travelpayouts"
TITLE = "Aviasales"
KIND = "flight"

BASE = "https://api.travelpayouts.com"

AIRLINES = {
    "RT": "ЮВТ Аэро", "UT": "ЮТэйр", "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "FV": "Россия",
    "N4": "Nordwind", "WZ": "Red Wings", "5N": "Smartavia", "U6": "Уральские авиалинии", "A4": "Азимут", "YC": "Ямал",
}


def airline_name(code: str | None) -> str:
    return AIRLINES.get(code or "", code or "")


def check_env(env: dict) -> str | None:
    if not env.get("TRAVELPAYOUTS_TOKEN"):
        return "Не задан секрет TRAVELPAYOUTS_TOKEN (Settings → Secrets and variables → Actions)"
    return None


def parse_flights(payload: object, direct_only: bool = True) -> list[dict]:
    if not isinstance(payload, dict):
        raise SourceError("Travelpayouts: неожиданный формат ответа", "schema")
    if payload.get("success") is False:
        msg = str(payload.get("error") or "ошибка")
        import re

        raise SourceError(f"Travelpayouts: {msg}", "auth" if re.search(r"token|auth|unauthor|access", msg, re.I) else "api")
    data = payload.get("data")
    if isinstance(data, dict):
        data = [x for v in data.values() for x in (v if isinstance(v, list) else [v])] if data else []
    if not isinstance(data, list):
        raise SourceError("Travelpayouts: в ответе нет списка предложений", "schema")
    offers = []
    for o in data:
        if not isinstance(o, dict):
            continue
        try:
            price = float(o.get("price", o.get("value")))
        except (TypeError, ValueError):
            continue
        if not price > 0:
            continue
        airline = str(o["airline"]) if o.get("airline") else ""
        number = str(o["flight_number"]) if o.get("flight_number") is not None else ""
        try:
            transfers = int(o.get("transfers") or 0)
        except (TypeError, ValueError):
            transfers = 0
        if direct_only and transfers != 0:
            continue
        try:
            duration = int(o.get("duration_to") or o.get("duration") or 0) or None
        except (TypeError, ValueError):
            duration = None
        offers.append({
            "key": f"{airline}{number}@{o.get('departure_at') or ''}",
            "airline": airline,
            "airline_name": airline_name(airline),
            "flight_number": f"{airline}{number}" if airline or number else "",
            "price": int(round(price)),
            "departure_at": str(o["departure_at"]) if o.get("departure_at") else None,
            "transfers": transfers,
            "origin_airport": o.get("origin_airport") or o.get("origin"),
            "destination_airport": o.get("destination_airport") or o.get("destination"),
            "duration_min": duration,
            "link": f"https://www.aviasales.ru{o['link']}" if o.get("link") else None,
        })
    offers.sort(key=lambda x: x["price"])
    return offers


def search_flights(origin: str, destination: str, date: str, env: dict, *, direct_only: bool = True, currency: str = "rub", session=None) -> dict:
    token = env.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        raise SourceError("Не задан секрет TRAVELPAYOUTS_TOKEN", "config")
    params = {
        "origin": origin, "destination": destination, "departure_at": date, "one_way": "true",
        "direct": "true" if direct_only else "false", "sorting": "price", "limit": "100", "page": "1",
        "currency": currency, "market": "ru",
    }
    base = (env.get("TP_BASE_URL") or BASE).rstrip("/")
    payload, meta, text = get_json(f"{base}/aviasales/v3/prices_for_dates", params=params, headers={"X-Access-Token": token, "Accept": "application/json"}, session=session)
    return {"offers": parse_flights(payload, direct_only), "meta": meta, "excerpt": text[:1500], "raw": payload}


def fetch(cfg, date: str, ctx: dict) -> dict:
    f = cfg.flights
    res = search_flights(f.origin, f.destination, date, ctx["env"], direct_only=f.direct_only, currency=f.currency, session=ctx.get("session"))
    return {"offers": res["offers"], "meta": res["meta"], "excerpt": res["excerpt"]}
