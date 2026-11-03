"""Источник поездов: ticket.rzd.ru (неофициальный JSON-API, тот же, что использует сайт).

Протокол взят из открытого клиента github.com/drGOD/rzd-api:
  GET /api/v1/suggests                             — подсказки станций (коды Экспресс-3)
  GET /api/v1/railway-service/prices/train-pricing — поезда на дату с группами вагонов и ценами

ВАЖНО: это не публичный контракт и он НЕ проверен на живых данных. Формат может измениться, поэтому разбор терпим
к вариантам названий полей, а любое расхождение превращается в понятную ошибку (SourceError kind=schema),
а не в тихое «билетов нет». Проверка на живых данных: Actions → «Проверка билетов» → Run workflow → probe.
К серверам GitHub (США) РЖД может не отвечать: тогда задайте секрет RZD_PROXY (российский прокси) — см. README.
"""
from __future__ import annotations

import datetime as dt
import os

from dozor.http import BROWSER_HEADERS, get_json
from dozor.util import SourceError, norm_name

NAME = "rzd"
TITLE = "РЖД"
KIND = "train"

BASE = "https://ticket.rzd.ru"
MOSCOW_CITY_CODE = "2000000"  # «Москва» целиком (все вокзалы)
CACHE_DAYS = 30

CAR_TYPES = {
    "ReservedSeat": "Плацкарт", "Plaz": "Плацкарт", "Reserved": "Плацкарт", "Compartment": "Купе", "Soft": "СВ",
    "Luxury": "Люкс", "Lux": "Люкс", "Sedentary": "Сидячий", "Seat": "Сидячий", "Shared": "Общий", "Common": "Общий",
    "Baggage": "Багажный",
}
NAME_KEYS = ("NameRu", "nameRu", "name", "Name", "n", "title")
CODE_KEYS = ("ExpressCode", "expressCode", "code", "Code", "c")


def base_url(env: dict) -> str:
    return (env.get("RZD_BASE_URL") or BASE).rstrip("/")


def headers() -> dict:
    return {**BROWSER_HEADERS, "Referer": f"{BASE}/"}


def check_env(env: dict) -> str | None:
    return None  # ключей не нужно


# ───────────────────────── мелкие помощники ─────────────────────────

def _s(v: object) -> str | None:
    if v is None or v == "" or isinstance(v, (dict, list)):
        return None
    return str(v)


def _n(v: object) -> float | None:
    if v is None or v == "" or isinstance(v, (bool, dict, list)):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _price(v: object) -> int | float | None:
    n = _n(v)
    return None if n is None else int(n) if n == int(n) else n


def _i(v: object) -> int | None:
    n = _n(v)
    return None if n is None else int(n)


def _pick(node: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = _s(node.get(k))
        if v is not None:
            return v
    return None


def _first(node: dict, *keys: str) -> object:
    for k in keys:
        if k in node and node[k] is not None:
            return node[k]
    return None


# ───────────────────────── подсказки станций ─────────────────────────

def _suggestion_nodes(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise SourceError("Подсказки станций РЖД: неожиданный формат ответа", "schema")
    for key in ("transport_node_suggests", "suggestions", "items", "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    cats = ("city", "train", "suburban", "bus")
    if any(isinstance(payload.get(k), list) for k in cats):
        out = []
        for k in cats:
            for n in payload.get(k) or []:
                out.append({**n, "__cat": k} if isinstance(n, dict) else n)
        return out
    return []


def _walk(node: object, out: list) -> None:
    if not isinstance(node, dict):
        return
    name = _pick(node, NAME_KEYS)
    codes = node.get("Codes") if isinstance(node.get("Codes"), dict) else node.get("codes") if isinstance(node.get("codes"), dict) else None
    code = _pick(node, CODE_KEYS) or (_s(codes.get("Railway")) if codes else None)
    if name and code:
        out.append({
            "name": name,
            "code": code,
            "kind": _s(node.get("SubType")) or _s(node.get("nodeType")) or _s(node.get("__cat")) or "",
            "region": _pick(node, ("region", "Region", "Description")) or "",
        })
        return
    for key in ("stations", "items", "children", "Children", "Stations"):
        if isinstance(node.get(key), list):
            for n in node[key]:
                _walk(n, out)


def parse_suggestions(payload: object) -> list[dict]:
    out: list[dict] = []
    for n in _suggestion_nodes(payload):
        _walk(n, out)
    seen, uniq = set(), []
    for s in out:
        k = (s["name"], s["code"], s["kind"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq


def pick_station(candidates: list[dict], query: str, prefer: str = "station") -> dict | None:
    """prefer='station' — конкретная станция отправления; 'city' — город назначения (все вокзалы)."""
    import re

    q = norm_name(query)
    exact = [s for s in candidates if norm_name(s["name"]) == q]
    pool = exact or [s for s in candidates if norm_name(s["name"]).startswith(q)]
    if not pool:
        return None

    def is_city(s: dict) -> bool:
        return bool(re.search(r"город|city", s["kind"], re.I))

    def is_station(s: dict) -> bool:
        return bool(re.search(r"станц|station|train|вокзал", s["kind"], re.I))

    def score(s: dict) -> int:
        main = (is_city(s) if prefer == "city" else is_station(s))
        return (2 if main else 0) + (1 if is_city(s) or is_station(s) else 0)

    return sorted(pool, key=score, reverse=True)[0]


def suggest_stations(query: str, env: dict, session=None) -> tuple[list[dict], dict]:
    params = {"Query": query, "TransportType": "rail,suburban", "GroupResults": "true", "RailwaySortPriority": "true", "SynonymOn": "1", "Language": "ru"}
    payload, meta, _ = get_json(f"{base_url(env)}/api/v1/suggests", params=params, headers=headers(), proxy=env.get("RZD_PROXY"), session=session)
    return parse_suggestions(payload), meta


def resolve_route(train, cache: dict, env: dict, now: int, session=None) -> dict:
    """Коды станций отправления/назначения; найденное кэшируется на 30 суток в cache (изменяется на месте)."""
    out = {}
    for side, name, manual, prefer in (("from", train.from_name, train.from_code, "station"), ("to", train.to_name, train.to_code, "city")):
        if manual:
            out[side] = {"code": str(manual), "name": name, "source": "manual"}
            continue
        key = f"{norm_name(name)}|{prefer}"
        hit = cache.get(key)
        if hit and now - hit["at"] < CACHE_DAYS * 86400:
            out[side] = {**hit, "source": "cache"}
            continue
        candidates, _ = suggest_stations(name, env, session)
        found = pick_station(candidates, name, prefer)
        if not found and norm_name(name) == "москва":
            found = {"name": "Москва", "code": MOSCOW_CITY_CODE, "kind": "fallback"}
        if not found:
            raise SourceError(f"Не нашёл станцию «{name}» в подсказках РЖД. Укажите её код вручную в config.yaml (train.{'from' if side == 'from' else 'to'}_code).", "schema")
        cache[key] = {"code": found["code"], "name": found["name"], "kind": found["kind"], "at": now}
        out[side] = {**cache[key], "source": "suggest"}
    return out


# ───────────────────────── поиск поездов ─────────────────────────

def _assert_no_api_error(p: object) -> None:
    if not isinstance(p, dict):
        return
    info = p.get("errorInfo")
    if isinstance(info, dict) and str(info.get("Code", "0")) != "0":
        raise SourceError(f"РЖД вернул ошибку {info.get('Code')}: {info.get('Message', '')}".strip(), "api")
    if isinstance(p.get("message"), str) and p["message"]:
        raise SourceError(f"РЖД: {p['message']}", "api")
    code = p.get("Code", p.get("code"))
    if code is not None and str(code) not in ("", "0"):
        raise SourceError(f"РЖД вернул код {code}: {p.get('Message', '')}".strip(), "api")
    if p.get("error"):
        err = p["error"]
        raise SourceError(f"РЖД: {err if isinstance(err, str) else str(err)[:120]}", "api")


def _category_sum(g: dict) -> int | None:
    keys = ("LowerPlaceQuantity", "UpperPlaceQuantity", "SideLowerPlaceQuantity", "SideUpperPlaceQuantity")
    present = [g.get(k, g.get(k[0].lower() + k[1:])) for k in keys]
    present = [v for v in present if v is not None]
    if not present:
        return None
    nums = [_i(v) for v in present]
    return None if any(n is None for n in nums) else sum(nums)


def _parse_group(g: dict) -> dict:
    typ = _s(_first(g, "CarType", "carType", "Type", "type")) or ""
    name = _s(_first(g, "CarTypeName", "carTypeName"))
    cls = _s(_first(g, "ServiceClassNameRu", "ServiceClass", "serviceClass"))
    seats = None
    for k in ("TotalPlaceQuantity", "totalPlaceQuantity", "PlaceQuantity", "placeQuantity", "FreePlaces", "freePlaces"):
        seats = _i(g.get(k))
        if seats is not None:
            break
    if seats is None:
        seats = _category_sum(g)
    min_price = _price(_first(g, "MinPrice", "minPrice", "Price", "price"))
    max_price = _price(_first(g, "MaxPrice", "maxPrice"))
    label = name or CAR_TYPES.get(typ) or typ or "Места"
    return {"key": f"{typ or label}|{cls or ''}", "label": f"{label} {cls}" if cls and cls not in label else label, "seats": seats, "min_price": min_price, "max_price": max_price}


def _merge_groups(groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for g in groups:
        cur = merged.get(g["key"])
        if cur is None:
            merged[g["key"]] = dict(g)
            continue
        cur["seats"] = None if cur["seats"] is None and g["seats"] is None else (cur["seats"] or 0) + (g["seats"] or 0)
        for fld, fn in (("min_price", min), ("max_price", max)):
            a, b = cur[fld], g[fld]
            cur[fld] = b if a is None else a if b is None else fn(a, b)
    return list(merged.values())


def _parse_train(n: dict) -> dict | None:
    display = _s(_first(n, "DisplayTrainNumber", "displayTrainNumber"))
    number = _s(_first(n, "TrainNumber", "trainNumber")) or display
    if not number:
        return None
    raw = _first(n, "CarGroups", "carGroups")
    if raw is not None and not isinstance(raw, list):
        raise SourceError(f"РЖД: у поезда {number} список вагонов не массив — формат мог измениться (запустите probe)", "schema")
    has_groups = isinstance(raw, list)
    groups = _merge_groups([_parse_group(g) for g in (raw or []) if isinstance(g, dict)])
    for g in groups:
        if g["seats"] is None:  # группа есть, число мест не распознали: лучше ложная тревога, чем пропуск
            g["seats"], g["approx"] = 1, True
    seats = sum(max(0, g["seats"]) for g in groups)
    prices = [g["min_price"] for g in groups if g["seats"] > 0 and g["min_price"] is not None]
    return {
        "number": number,
        "display_number": display or number,
        "from": _s(_first(n, "OriginStationName", "originStationName")),
        "to": _s(_first(n, "DestinationStationName", "destinationStationName")),
        "depart_local": _s(_first(n, "LocalDepartureDateTime", "localDepartureDateTime", "DepartureDateTime", "departureDateTime")),
        "arrive_local": _s(_first(n, "LocalArrivalDateTime", "localArrivalDateTime", "ArrivalDateTime", "arrivalDateTime")),
        "brand": _s(_first(n, "TrainBrand", "Brand", "TrainName")),
        "seats": seats,
        "min_price": min(prices) if prices else None,
        "no_data": not has_groups,  # РЖД показал поезд (например, из расписания), но данных о местах нет
        "groups": [g for g in groups if g["seats"] > 0],
    }


def parse_train_pricing(payload: object) -> dict:
    """Ответ train-pricing → {trains, partial, received}. Бросает SourceError(schema|api) при непонятном формате."""
    _assert_no_api_error(payload)
    root = payload
    if isinstance(root, dict) and isinstance(root.get("data"), dict):
        root = root["data"]
    if isinstance(root, list):
        lst = root
    elif isinstance(root, dict):
        lst = root.get("Trains", root.get("trains"))
    else:
        lst = None
    if not isinstance(lst, list):
        raise SourceError("Ответ РЖД без списка поездов — формат мог измениться (запустите probe)", "schema")
    trains = [t for t in (_parse_train(x) for x in lst if isinstance(x, dict)) if t]
    if lst and not trains:
        raise SourceError("Ответ РЖД содержит записи, но ни в одной нет номера поезда — формат мог измениться (запустите probe)", "schema")
    trains.sort(key=lambda t: str(t["depart_local"]))
    return {"trains": trains, "partial": isinstance(root, dict) and root.get("NotAllTrainsReturned") is True, "received": len(lst)}


def search_trains(from_code: str, to_code: str, date: str, env: dict, adults: int = 1, session=None) -> dict:
    params = {
        "service_provider": "B2B_RZD", "getByLocalTime": "true", "carGrouping": "DontGroup",
        "origin": str(from_code), "destination": str(to_code), "departureDate": f"{date}T00:00:00",
        "specialPlacesDemand": "StandardPlacesAndForDisabledPersons", "carIssuingType": "Passenger",
        "getTrainsFromSchedule": "true", "adultPassengersQuantity": str(adults), "childrenPassengersQuantity": "0",
        "hasPlacesForLargeFamily": "false",
    }
    payload, meta, text = get_json(f"{base_url(env)}/api/v1/railway-service/prices/train-pricing", params=params, headers=headers(), proxy=env.get("RZD_PROXY"), session=session)
    try:
        parsed = parse_train_pricing(payload)
    except SourceError as exc:
        exc.meta = {**meta, "excerpt": text[:1500]}  # по началу ответа видно, что именно изменилось
        raise
    return {**parsed, "meta": meta, "excerpt": text[:1500], "raw": payload}


# ───────────────────────── интерфейс источника ─────────────────────────

def fetch(cfg, date: str, ctx: dict) -> dict:
    """Снимок поездов на дату. ctx: env, state (изменяется: station_cache), now, session."""
    env = ctx["env"]
    cache = ctx["state"].setdefault("station_cache", {})
    route = resolve_route(cfg.train, cache, env, ctx["now"], ctx.get("session"))
    res = search_trains(route["from"]["code"], route["to"]["code"], date, env, session=ctx.get("session"))
    return {
        "trains": res["trains"],
        "partial": res["partial"],
        "route": {"from": route["from"], "to": route["to"]},
        "meta": res["meta"],
        "excerpt": res["excerpt"],
    }
