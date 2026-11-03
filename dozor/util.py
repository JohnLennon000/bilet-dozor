"""Форматирование дат, сумм и мелкие помощники. Время везде — целые секунды Unix (UTC)."""
from __future__ import annotations

import datetime as dt
import re

MSK = dt.timezone(dt.timedelta(hours=3))  # в Москве нет перехода на летнее время
NBSP = " "
MINUS = "−"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class SourceError(Exception):
    """Ошибка источника данных. kind: network | blocked | rate | auth | server | http | schema | api | config."""

    def __init__(self, message: str, kind: str = "http", meta: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.meta = meta or {}


def now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> int:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def msk(ts: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, MSK)


def today_msk(ts: int | float) -> str:
    return msk(ts).date().isoformat()


def stamp(ts: int | float) -> str:
    """«03.11 12:05» по Москве."""
    return msk(ts).strftime("%d.%m %H:%M")


def is_iso_date(text: str) -> bool:
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


# ───────────────────────── числа и слова ─────────────────────────

def num(n: int | float) -> str:
    return f"{int(round(n)):,}".replace(",", NBSP)


def rub(n: int | float) -> str:
    return f"{num(n)}{NBSP}₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many


def seats_word(n: int) -> str:
    return f"{n} {plural(n, 'место', 'места', 'мест')}"


def signed(n: int | float) -> str:
    r = int(round(n))
    sign = "+" if r > 0 else MINUS if r < 0 else ""
    return f"{sign}{num(abs(r))}"


def pct_str(old: float, new: float) -> str:
    if not old:
        return ""
    v = (new - old) / old * 100
    body = str(int(round(abs(v)))) if abs(v) >= 10 else f"{abs(v):.1f}".replace(".", ",")
    sign = "+" if v > 0 else MINUS if v < 0 else ""
    return f"{sign}{body}%"


# ───────────────────────── даты ─────────────────────────

def fmt_date(day: str) -> str:
    """'2027-01-09' → '09.01.2027 (сб)'"""
    d = dt.date.fromisoformat(day)
    return f"{d.day:02d}.{d.month:02d}.{d.year} ({WEEKDAYS[d.weekday()]})"


def fmt_short_date(day: str) -> str:
    """'2027-01-09' → '09.01'"""
    _, m, d = day.split("-")
    return f"{d}.{m}"


def clock(local_iso: str | None) -> str:
    """Время из «наивной» локальной строки '2027-01-09T05:47:00' → '05:47'."""
    m = re.search(r"T(\d{2}:\d{2})", str(local_iso or ""))
    return m.group(1) if m else ""


def clock_msk(iso_text: str | None) -> str:
    """Время из строки со смещением → 'ЧЧ:ММ' по Москве."""
    if not iso_text:
        return ""
    try:
        return msk(parse_iso(iso_text)).strftime("%H:%M")
    except ValueError:
        return ""


# ───────────────────────── названия ─────────────────────────

def norm_name(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[«»\"']", "", str(s or "").lower().replace("ё", "е"))).strip()


def title_case(s: str | None) -> str:
    return re.sub(r"(^|[\s\-(])(\w)", lambda m: m.group(1) + m.group(2).upper(), str(s or "").lower())


_HOMOGLYPHS = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У"})


def norm_train(s: str | None) -> str:
    """«301y» (латиницей) и «301У» — один и тот же поезд."""
    return re.sub(r"\s+", "", str(s or "").upper()).translate(_HOMOGLYPHS)


_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slug(name: str) -> str:
    """«Туймазы» → «tuymazy» (так строятся адреса на Яндекс Путешествиях и Туту)."""
    text = "".join(_TR.get(ch, ch) for ch in str(name).lower())
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", text))


IATA_NAMES = {
    "UUA": "Бугульма", "MOW": "Москва", "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково",
    "ZIA": "Жуковский", "KZN": "Казань", "UFA": "Уфа", "LED": "Санкт-Петербург", "GOJ": "Нижний Новгород",
    "NBC": "Нижнекамск", "OSW": "Оренбург",
}


def iata_name(code: str) -> str:
    return IATA_NAMES.get(code, code)
