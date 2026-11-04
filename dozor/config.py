"""Чтение и проверка config.yaml. Все сообщения об ошибках — по-русски: их увидит владелец на телефоне."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dozor.util import is_iso_date

TRAIN_SOURCES = ("rzd",)
FLIGHT_SOURCES = ("travelpayouts",)

DEFAULT_TRAIN_LINKS = [
    {"label": "Купить на РЖД", "url": "https://ticket.rzd.ru/searchresults/v/1/{from_code}/{to_code}/{date}", "primary": True},
    {"label": "Яндекс Путешествия", "url": "https://travel.yandex.ru/trains/{from_slug}--{to_slug}/?when={date}"},
    {"label": "Туту", "url": "https://www.tutu.ru/poezda/{From_slug}/{To_slug}/"},
]
DEFAULT_FLIGHT_LINKS = [
    {"label": "Найти на Aviasales", "url": "https://www.aviasales.ru/search/{origin}{dd}{mm}{destination}1", "primary": True},
]


class ConfigError(ValueError):
    pass


@dataclass
class Train:
    enabled: bool = True
    from_name: str = "Туймазы"
    to_name: str = "Москва"
    from_code: str = ""
    to_code: str = ""
    dates: list[str] = field(default_factory=lambda: ["2027-01-09"])
    trains: list[str] = field(default_factory=list)  # пусто = любой прямой поезд
    sources: list[str] = field(default_factory=lambda: ["rzd"])
    links: list[dict] = field(default_factory=lambda: copy.deepcopy(DEFAULT_TRAIN_LINKS))
    label: str = ""   # пометка в карточке и уведомлениях, например «эксперимент» (для extra_trains)


@dataclass
class Flights:
    enabled: bool = True
    origin: str = "UUA"
    destination: str = "MOW"
    dates: list[str] = field(default_factory=lambda: ["2027-01-09", "2027-01-10"])
    direct_only: bool = True
    interval_min: int = 30
    currency: str = "rub"
    sources: list[str] = field(default_factory=lambda: ["travelpayouts"])
    links: list[dict] = field(default_factory=lambda: copy.deepcopy(DEFAULT_FLIGHT_LINKS))


@dataclass
class Notify:
    price_min_rub: int = 50
    price_min_pct: float = 1.0
    price_gap_min: int = 10
    sold_out: bool = True
    new_train: bool = True
    digest_hour: int | None = 9
    error_after_min: int = 90
    quiet_from: int | None = 23  # без звука для необязательных уведомлений: с 23:00 до 8:00 по Москве
    quiet_to: int | None = 8

    def is_quiet(self, hour: int) -> bool:
        if self.quiet_from is None or self.quiet_to is None or self.quiet_from == self.quiet_to:
            return False
        if self.quiet_from < self.quiet_to:
            return self.quiet_from <= hour < self.quiet_to
        return hour >= self.quiet_from or hour < self.quiet_to


@dataclass
class Config:
    train: Train = field(default_factory=Train)
    extra_trains: list[Train] = field(default_factory=list)   # дополнительные маршруты поезда
    flights: Flights = field(default_factory=Flights)
    notify: Notify = field(default_factory=Notify)

    def train_routes(self) -> list[tuple[str, Train]]:
        """(префикс ключа, маршрут): основной — «train», дополнительные — «train2», «train3»…"""
        return [("train", self.train)] + [(f"train{i + 2}", t) for i, t in enumerate(self.extra_trains)]


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"«{name}» должен быть блоком с настройками")
    return value


def _check_keys(section: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"{where}: неизвестная настройка «{unknown[0]}». Допустимые: {', '.join(sorted(allowed))}")


def _bool(value: object, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where}: нужно true или false")


def _int(value: object, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ConfigError(f"{where}: нужно целое число")
    if not lo <= int(value) <= hi:
        raise ConfigError(f"{where}: от {lo} до {hi}")
    return int(value)


def _text(value: object, where: str, max_len: int = 60) -> str:
    text = str(value if value is not None else "").strip()
    if not text or len(text) > max_len:
        raise ConfigError(f"{where}: от 1 до {max_len} символов")
    return text


def _dates(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ConfigError(f"{where}: список от 1 до 10 дат вида 2027-01-09")
    out = []
    for item in value:
        text = item.isoformat() if isinstance(item, (dt.date, dt.datetime)) else str(item).strip()  # YAML сам превращает 2027-01-09 в дату
        if not is_iso_date(text):
            raise ConfigError(f"{where}: «{text}» — неверная дата, нужен формат 2027-01-09")
        if text not in out:
            out.append(text)
    return sorted(out)


def _sources(value: object, where: str, known: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: список источников, например [{known[0]}]")
    for item in value:
        if item not in known:
            raise ConfigError(f"{where}: неизвестный источник «{item}». Доступны: {', '.join(known)}")
    return list(dict.fromkeys(value))


def _links(value: object, where: str) -> list[dict]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: список ссылок вида  - {{label: Туту, url: \"https://...\"}}")
    out = []
    for item in value:
        if not isinstance(item, dict) or "label" not in item or "url" not in item:
            raise ConfigError(f"{where}: у каждой ссылки нужны label и url")
        url = str(item["url"]).strip()
        if not url.startswith("https://") or any(c.isspace() for c in url):
            raise ConfigError(f"{where}: ссылка «{item['label']}» должна начинаться с https://")
        out.append({"label": _text(item["label"], f"{where}: название ссылки", 40), "url": url, "primary": bool(item.get("primary", not out))})
    if sum(1 for x in out if x["primary"]) > 1:
        first = next(i for i, x in enumerate(out) if x["primary"])
        for i, x in enumerate(out):
            x["primary"] = i == first
    return out


def _parse_train(t: dict, route: Train, where: str) -> None:
    _check_keys(t, {"enabled", "from", "to", "from_code", "to_code", "dates", "trains", "sources", "links", "label"}, where)
    if "enabled" in t:
        route.enabled = _bool(t["enabled"], f"{where}.enabled")
    if "from" in t:
        route.from_name = _text(t["from"], f"{where}.from")
    if "to" in t:
        route.to_name = _text(t["to"], f"{where}.to")
    for key in ("from_code", "to_code"):
        if key in t and str(t[key] or "").strip():
            code = str(t[key]).strip()
            if not code.isdigit() or not 4 <= len(code) <= 9:
                raise ConfigError(f"{where}.{key}: код станции — только цифры")
            setattr(route, key, code)
    if "dates" in t:
        route.dates = _dates(t["dates"], f"{where}.dates")
    if "trains" in t:
        trains = t["trains"] or []
        if not isinstance(trains, list) or len(trains) > 10 or any(len(str(x)) > 12 for x in trains):
            raise ConfigError(f"{where}.trains: список до 10 номеров поездов, например [\"301У\"]")
        route.trains = [str(x).strip() for x in trains if str(x).strip()]
    if "sources" in t:
        route.sources = _sources(t["sources"], f"{where}.sources", TRAIN_SOURCES)
    if "links" in t:
        route.links = _links(t["links"], f"{where}.links")
    if t.get("label"):
        route.label = _text(t["label"], f"{where}.label", 30)


def parse_config(raw: object) -> Config:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml должен состоять из блоков train, flights, notify")
    _check_keys(raw, {"train", "extra_trains", "flights", "notify"}, "config.yaml")
    cfg = Config()

    _parse_train(_section(raw, "train"), cfg.train, "train")
    extra = raw.get("extra_trains") or []
    if not isinstance(extra, list) or len(extra) > 5:
        raise ConfigError("extra_trains: список до 5 маршрутов вида  - {from: Москва, to: Уруссу, dates: [2026-12-25]}")
    for i, item in enumerate(extra):
        where = f"extra_trains[{i + 1}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: нужен блок с настройками from, to, dates")
        for key in ("from", "to", "dates"):
            if key not in item:
                raise ConfigError(f"{where}: не указано «{key}»")
        route = Train()
        _parse_train(item, route, where)
        cfg.extra_trains.append(route)

    f = _section(raw, "flights")
    _check_keys(f, {"enabled", "origin", "destination", "dates", "direct_only", "interval_min", "currency", "sources", "links"}, "flights")
    if "enabled" in f:
        cfg.flights.enabled = _bool(f["enabled"], "flights.enabled")
    for key in ("origin", "destination"):
        if key in f:
            code = str(f[key] or "").strip().upper()
            if len(code) != 3 or not code.isalpha() or not code.isascii():
                raise ConfigError(f"flights.{key}: три латинские буквы (код IATA)")
            setattr(cfg.flights, key, code)
    if "dates" in f:
        cfg.flights.dates = _dates(f["dates"], "flights.dates")
    if "direct_only" in f:
        cfg.flights.direct_only = _bool(f["direct_only"], "flights.direct_only")
    if "interval_min" in f:
        cfg.flights.interval_min = _int(f["interval_min"], "flights.interval_min", 5, 1440)
    if "currency" in f:
        cur = str(f["currency"]).lower()
        if cur not in ("rub", "usd", "eur"):
            raise ConfigError("flights.currency: rub, usd или eur")
        cfg.flights.currency = cur
    if "sources" in f:
        cfg.flights.sources = _sources(f["sources"], "flights.sources", FLIGHT_SOURCES)
    if "links" in f:
        cfg.flights.links = _links(f["links"], "flights.links")

    n = _section(raw, "notify")
    _check_keys(n, {"price_min_rub", "price_min_pct", "price_gap_min", "sold_out", "new_train", "digest_hour", "error_after_min", "quiet_from", "quiet_to"}, "notify")
    if "price_min_rub" in n:
        cfg.notify.price_min_rub = _int(n["price_min_rub"], "notify.price_min_rub", 0, 100000)
    if "price_min_pct" in n:
        v = n["price_min_pct"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise ConfigError("notify.price_min_pct: число от 0 до 100")
        cfg.notify.price_min_pct = float(v)
    if "price_gap_min" in n:
        cfg.notify.price_gap_min = _int(n["price_gap_min"], "notify.price_gap_min", 0, 1440)
    for key in ("sold_out", "new_train"):
        if key in n:
            setattr(cfg.notify, key, _bool(n[key], f"notify.{key}"))
    for key, lo, hi in (("digest_hour", 0, 23), ("quiet_from", 0, 23), ("quiet_to", 0, 23)):
        if key in n:
            setattr(cfg.notify, key, None if n[key] in (None, False) else _int(n[key], f"notify.{key}", lo, hi))
    if "error_after_min" in n:
        cfg.notify.error_after_min = _int(n["error_after_min"], "notify.error_after_min", 10, 1440)
    return cfg


def load_config(path: Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Не найден файл {path}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (строка {mark.line + 1})" if mark else ""
        raise ConfigError(f"config.yaml не читается{where}: проверьте отступы и кавычки") from None
    return parse_config(raw)
