"""Живая проверка источников: python -m dozor probe [--raw ПАПКА] [--date ГГГГ-ММ-ДД]

Запускается в Actions (Run workflow → probe = true) или на компьютере/телефоне. Показывает, отвечает ли РЖД
с этого адреса, как называются нужные станции и разбирается ли ответ. Ничего не пишет в данные сервиса.
Код выхода 0 — РЖД ответил и ответ разобран; 1 — есть проблема (подсказки в выводе).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

from dozor.config import ConfigError, load_config
from dozor.http import BROWSER_HEADERS, call_with_deadline
from dozor.diff import train_matches
from dozor.sources import rzd, travelpayouts
from dozor.util import SourceError, clock, clock_msk, fmt_date, rub, seats_word, title_case

HINTS = {
    "blocked": "РЖД закрыл доступ с этого адреса. Серверы GitHub находятся в США, и сайт РЖД мог их заблокировать. "
               "Задайте секрет RZD_PROXY (адрес российского прокси) — см. README, раздел «РЖД не отвечает».",
    "network": "Нет связи с сайтом РЖД (таймаут или сброс соединения) — для зарубежных адресов это типично. См. README, «РЖД не отвечает».",
    "rate": "РЖД просит не частить. Подождите и повторите.",
    "server": "Сбой на стороне РЖД. Повторите позже.",
    "schema": "Формат ответа РЖД отличается от ожидаемого. Скопируйте блок «Фрагмент ответа» ниже и пришлите разработчику — парсер поправят.",
    "api": "РЖД вернул ошибку в теле ответа (см. текст выше).",
    "http": "Неожиданный HTTP-ответ РЖД (см. выше).",
}


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []   # для консоли
        self.md: list[str] = []      # для $GITHUB_STEP_SUMMARY
        self.failed = False

    def head(self, text: str) -> None:
        print(f"\n── {text} " + "─" * max(2, 60 - len(text)))
        self.md.append(f"\n### {text}\n")

    def ok(self, text: str) -> None:
        print(f"✅ {text}")
        self.md.append(f"- ✅ {text}")

    def bad(self, text: str) -> None:
        print(f"❌ {text}")
        self.md.append(f"- ❌ {text}")
        self.failed = True

    def warn(self, text: str) -> None:
        print(f"⚠️  {text}")
        self.md.append(f"- ⚠️ {text}")

    def info(self, text: str) -> None:
        print(f"   {text}")
        self.md.append(f"  - {text}")

    def block(self, title: str, text: str) -> None:
        print(f"\n   {title}:\n" + "\n".join("   | " + ln for ln in text.splitlines()))
        self.md.append(f"\n<details><summary>{title}</summary>\n\n```\n{text}\n```\n</details>\n")


def save_raw(directory: Path | None, name: str, payload: object) -> None:
    if not directory:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   сырой ответ сохранён: {directory / name}")


REACH = (
    ("РЖД, главная", "https://ticket.rzd.ru/"),
    ("Туту", "https://www.tutu.ru/poezda/"),
    ("Яндекс Путешествия", "https://travel.yandex.ru/trains/"),
    ("Т-Банк Путешествия", "https://www.tbank.ru/travel/trains/"),
    ("Озон Трэвел", "https://www.ozon.travel/"),
    ("OneTwoTrip", "https://www.onetwotrip.com/ru/poezda/"),
    ("Aviasales (для сравнения)", "https://www.aviasales.ru/"),
)


def reach(rep: Report, proxy: str | None) -> None:
    """Достучаться до главных страниц: отвечает ли сайт серверу, пускает ли или показывает заглушку."""
    rep.head("Доступность сайтов с этого адреса")
    proxies = {"https": proxy, "http": proxy} if proxy else None
    for name, url in REACH:
        t0 = time.monotonic()
        try:
            r = call_with_deadline(lambda: requests.get(url, headers={**BROWSER_HEADERS, "Accept": "text/html,*/*"}, timeout=8, proxies=proxies), 12)
        except Exception as exc:  # noqa: BLE001
            kind = "нет ответа за 12 с" if type(exc).__name__ in ("_Deadline", "ReadTimeout", "ConnectTimeout") else type(exc).__name__
            rep.warn(f"{name}: {kind}")
            continue
        ms = int((time.monotonic() - t0) * 1000)
        low = r.text[:4000].lower()
        wall = any(w in low for w in ("captcha", "qrator", "access denied", "cloudflare", "доступ ограничен", "robot"))
        line = f"{name}: HTTP {r.status_code}, {len(r.content)} байт, {ms} мс{', похоже на защиту от ботов' if wall else ''}"
        (rep.ok if r.status_code < 400 and not wall else rep.warn)(line)


DISCOVER = (
    ("tutu-dated", "https://www.tutu.ru/poezda/Tuymazy/Moskva/?date=09.01.2027"),
)
TUTU_API = "https://offers-api.tutu.ru"


def _fetch(url: str, proxy: str | None, **kw):
    proxies = {"https": proxy, "http": proxy} if proxy else None
    return call_with_deadline(lambda: requests.get(url, headers={**BROWSER_HEADERS, "Accept": "text/html,*/*"}, timeout=10, proxies=proxies, **kw), 15)


def discover(rep: Report, root: Path, proxy: str | None) -> None:
    """Сохраняет в docs/data/discover/ страницу Туту на нужную дату и места в её скриптах, где описан обмен с offers-api."""
    import re

    rep.head("Разведка: Туту на дату 09.01.2027")
    out = root / "docs" / "data" / "discover"
    save = bool(os.environ.get("GITHUB_ACTIONS"))
    if save:
        out.mkdir(parents=True, exist_ok=True)
    page = ""
    for name, url in DISCOVER:
        try:
            r = _fetch(url, proxy)
        except Exception as exc:  # noqa: BLE001
            rep.warn(f"{name}: {type(exc).__name__}")
            continue
        page = r.text
        if save:
            (out / f"{name}.html").write_text(page[:500000], encoding="utf-8")
        rep.ok(f"{name}: HTTP {r.status_code}, {len(page)} знаков, итоговый адрес {r.url[:140]}")
        m = re.search(r'"incomingSearchParams":(\{.*?\}\]?[^{}]*?\}),"callCenterParams"', page, re.S)
        rep.info(f"поиск на странице: {m.group(1)[:300] if m else 'не найден'}")
    # скрипты приложения: ищем, как оно общается с offers-api
    chunks = sorted(set(re.findall(r'https://cdn1\.tu-tu\.ru/offers/+_next/static/[\w/%.\[\]-]+\.js', page)))
    rep.info(f"скриптов приложения: {len(chunks)}")
    base = "https://cdn1.tu-tu.ru/offers/"
    bid = re.search(r'"buildId":"([^"]+)"', page)
    if bid:   # страницы и скрипты, которые приложение подгружает позже
        try:
            man = _fetch(f"{base}_next/static/{bid.group(1)}/_buildManifest.js", proxy).text
            extra = re.search(r'"/unified\.train":\s*\[(.*?)\]', man, re.S)
            names = re.findall(r'"([^"]+\.js)"', extra.group(1)) if extra else []
            rep.info(f"из манифеста для /unified.train: {len(names)} скриптов")
            chunks = sorted(set(chunks) | {f"{base}_next/{n}" for n in names})
        except Exception as exc:  # noqa: BLE001
            rep.info(f"манифест скриптов: {type(exc).__name__}")
    total, kept = 0, 0
    pat = re.compile(r"searchDataHost|trainGatewayHost|train-gateway|new WebSocket|text/event-stream|socket\.io")
    for url in chunks[:90]:
        try:
            js = _fetch(url, proxy).text
        except Exception:  # noqa: BLE001
            continue
        total += len(js)
        if pat.search(js) and kept < 2_500_000:
            kept += len(js)
            if save:
                (out / "js").mkdir(exist_ok=True)
                (out / "js" / url.rsplit("/", 1)[-1]).write_text(js, encoding="utf-8")
            rep.info(f"сохранён скрипт {url.rsplit('/', 1)[-1]} ({len(js)} знаков)")
    rep.info(f"скачано {total} знаков скриптов, сохранено {kept}")
    # пробуем прямые обращения к offers-api
    for name, url in (("offers-api корень", TUTU_API + "/"), ("offers-api socket.io", TUTU_API + "/socket.io/?EIO=4&transport=polling")):
        try:
            r = _fetch(url, proxy)
            rep.info(f"{name}: HTTP {r.status_code}, {r.headers.get('content-type', '')[:40]}, начало: {r.text[:160]!r}")
        except Exception as exc:  # noqa: BLE001
            rep.info(f"{name}: {type(exc).__name__}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m dozor probe")
    parser.add_argument("--root", default=".")
    parser.add_argument("--date", help="проверить одну дату поезда")
    parser.add_argument("--raw", help="папка для сырых ответов")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        cfg = load_config(root / "config.yaml")
        print(f"Настройки: {root / 'config.yaml'}")
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 1
    env = {k: os.environ[k].strip() for k in ("TRAVELPAYOUTS_TOKEN", "RZD_PROXY", "RZD_BASE_URL", "TP_BASE_URL") if os.environ.get(k, "").strip()}
    raw_dir = Path(args.raw) if args.raw else None
    rep = Report()
    if env.get("RZD_PROXY"):
        rep.warn("Используется прокси из секрета RZD_PROXY")

    # --- РЖД ------------------------------------------------------------------------------
    rep.head("РЖД · станции")
    route = {}
    try:
        for side, name, prefer in (("from", cfg.train.from_name, "station"), ("to", cfg.train.to_name, "city")):
            if side == "from" and cfg.train.from_code or side == "to" and cfg.train.to_code:
                code = cfg.train.from_code if side == "from" else cfg.train.to_code
                route[side] = {"code": code, "name": name}
                rep.ok(f"«{name}»: код {code} задан вручную")
                continue
            candidates, meta = rzd.suggest_stations(name, env)
            save_raw(raw_dir, f"suggest-{side}.json", candidates)
            found = rzd.pick_station(candidates, name, prefer)
            if not found and name.lower() == "москва":
                found = {"name": "Москва", "code": rzd.MOSCOW_CITY_CODE, "kind": "fallback"}
            if not found:
                rep.bad(f"Станция «{name}» не найдена в подсказках РЖД ({meta['status']}, {meta['ms']} мс)")
                for c in candidates[:8]:
                    rep.info(f"кандидат: {c['name']} · код {c['code']} · {c['kind']}")
                rep.info("Укажите код вручную в config.yaml: train.from_code / train.to_code")
                continue
            route[side] = found
            rep.ok(f"«{name}» → {found['name']} · код {found['code']} · {found['kind'] or '—'} ({meta['status']}, {meta['ms']} мс)")
    except SourceError as exc:
        rep.bad(f"{exc}")
        rep.info(HINTS.get(exc.kind, ""))
        if exc.meta.get("excerpt"):
            rep.block("Фрагмент ответа", exc.meta["excerpt"])

    rep.head("РЖД · поезда")
    dates = [args.date] if args.date else cfg.train.dates
    if len(route) < 2:
        rep.warn("Без кодов станций поезда не проверить")
    else:
        for date in dates:
            try:
                res = rzd.search_trains(route["from"]["code"], route["to"]["code"], date, env)
            except SourceError as exc:
                rep.bad(f"{fmt_date(date)}: {exc}")
                rep.info(HINTS.get(exc.kind, ""))
                if exc.meta.get("excerpt"):
                    rep.block("Фрагмент ответа", exc.meta["excerpt"])
                continue
            save_raw(raw_dir, f"trains-{date}.json", res["raw"])
            trains = res["trains"]
            rep.ok(f"{fmt_date(date)}: ответ разобран, поездов {len(trains)} ({res['meta']['status']}, {res['meta']['ms']} мс){' · выдача неполная' if res['partial'] else ''}")
            if not trains:
                rep.info("РЖД не показал ни одного поезда на эту дату (для даты, до которой ещё далеко, это нормально)")
            for t in trains:
                mark = "★ " if train_matches(cfg.train.trains, t["number"]) and cfg.train.trains else ""
                state = seats_word(t["seats"]) if t["seats"] else ("нет данных о местах" if t["no_data"] else "мест нет")
                price = f", от {rub(t['min_price'])}" if t["min_price"] is not None else ""
                rep.info(f"{mark}{t['display_number']} {title_case(t['from'])} {clock(t['depart_local'])} → {title_case(t['to'])} {clock(t['arrive_local'])}: {state}{price}")
                for g in t["groups"]:
                    rep.info(f"    {g['label']}: {seats_word(g['seats'])}{' (число мест не распознано)' if g.get('approx') else ''}" + (f", от {rub(g['min_price'])}" if g["min_price"] is not None else ""))
            if res.get("excerpt"):
                rep.block(f"Фрагмент ответа {date} (первые 1500 символов)", res["excerpt"])

    # --- Aviasales --------------------------------------------------------------------------
    rep.head("Aviasales · рейсы")
    if not cfg.flights.enabled:
        rep.info("Рейсы выключены в config.yaml")
    elif not env.get("TRAVELPAYOUTS_TOKEN"):
        rep.warn("Не задан секрет TRAVELPAYOUTS_TOKEN — рейсы не проверяются")
    else:
        for date in cfg.flights.dates:
            try:
                res = travelpayouts.search_flights(cfg.flights.origin, cfg.flights.destination, date, env, direct_only=cfg.flights.direct_only, currency=cfg.flights.currency)
            except SourceError as exc:
                rep.bad(f"{fmt_date(date)}: {exc}")
                continue
            save_raw(raw_dir, f"flights-{date}.json", res["raw"])
            offers = res["offers"]
            if offers:
                top = offers[0]
                rep.ok(f"{fmt_date(date)}: предложений {len(offers)}, дешевле всего {rub(top['price'])} ({top['airline_name']} {top['flight_number']}, вылет {clock_msk(top['departure_at'])} МСК)")
            else:
                rep.ok(f"{fmt_date(date)}: источник отвечает, но прямых билетов в кэше Aviasales сейчас нет")

    reach(rep, env.get("RZD_PROXY"))
    discover(rep, root, env.get("RZD_PROXY"))

    print()
    if rep.failed:
        print("Итог: есть проблемы — см. строки с ❌ выше.")
    else:
        print("Итог: источники отвечают, ответы разобраны.")
    if os.environ.get("GITHUB_ACTIONS"):   # отчёт кладём в репозиторий: его видно без входа в журнал запуска
        out = root / "docs" / "data" / "probe.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "failed": rep.failed, "lines": rep.md}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Проверка источников (probe)\n" + "\n".join(rep.md) + "\n\n" + ("**Итог: есть проблемы.**" if rep.failed else "**Итог: источники отвечают, ответы разобраны.**") + "\n")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
