#!/usr/bin/env python3
"""Дозор билетов: краткое состояние из docs/data/summary.json.

Запуск из корня репозитория:  python3 .claude/skills/dozor-biletov/scripts/status.py [путь_к_summary.json]
Без аргумента читает свежую версию из origin/main (сначала сделайте git fetch origin main),
а если git недоступен — локальный docs/data/summary.json.
"""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

MSK = dt.timezone(dt.timedelta(hours=3))


def load() -> dict:
    if len(sys.argv) > 1:
        return json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        text = subprocess.run(["git", "show", "origin/main:docs/data/summary.json"], capture_output=True, text=True, check=True).stdout
        return json.loads(text)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return json.loads(Path("docs/data/summary.json").read_text(encoding="utf-8"))


def t(sec) -> str:
    return dt.datetime.fromtimestamp(sec, MSK).strftime("%d.%m %H:%M") if sec else "—"


def main() -> None:
    d = load()
    checked = d.get("checked_at", "")
    if checked:
        checked = dt.datetime.fromisoformat(checked.replace("Z", "+00:00")).astimezone(MSK).strftime("%d.%m %H:%M")
    print(f"Обновлено: {checked} МСК · телефонов с push: {len((d.get('push') or {}).get('devices') or [])}")
    print("\nНаблюдения:")
    for w in d.get("watches", []):
        mark = {"ok": "✅", "error": "⚠️", "pending": "…", "config": "⚙️"}.get(w["status"], w["status"])
        line = f"  {mark} {w['key']}  {w.get('subtitle', '')}{' · ' + w['label'] if w.get('label') else ''}  данные от {t(w.get('last_ok_at'))}"
        if w.get("error"):
            line += f"  ОШИБКА: {w['error']}"
        print(line)
        for tr in w.get("trains") or []:
            groups = ", ".join(f"{g['label']} {g['seats']} от {g['min_price']}" for g in tr.get("groups") or [])
            print(f"      {tr.get('display_number') or tr['number']}: мест {tr['seats']}{' (' + groups + ')' if groups else ''}")
        for o in (w.get("offers") or [])[:3]:
            print(f"      ✈ {o.get('airline_name') or o.get('airline')} {o.get('flight_number')}: {o['price']} ₽")
    print("\nИсточники:")
    for name, s in (d.get("sources") or {}).items():
        ok, err = s.get("last_ok") or {}, s.get("last_error") or {}
        bad = err.get("at", 0) > ok.get("at", 0)
        print(f"  {s.get('title', name)}: последний ответ {t(ok.get('at'))}" + (f"; ОШИБКА {t(err.get('at'))}: {err.get('message')}" if bad else ""))
    print("\nПоследние события:")
    for e in (d.get("events") or [])[:6]:
        print(f"  {t(e.get('t'))}  {e['title']}")


if __name__ == "__main__":
    main()
