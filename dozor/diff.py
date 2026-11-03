"""Чистая логика: что изменилось между двумя снимками и о чём стоит сообщить. Ввода-вывода здесь нет.

Правила для поездов:
  • 0 мест → есть места           — «Появились билеты» (срочно)
  • новый класс вагонов с местами — «Появились места: …» (срочно)
  • есть места → 0                — «Билеты закончились»
  • цена класса отличается от ПОСЛЕДНЕЙ СООБЩЁННОЙ на порог — «Цена изменилась» (с динамикой)
  • новый поезд на дату           — «Новый поезд» (срочно, если сразу с местами)
Цены сравниваются с «базовой» — той, о которой уже сообщили. Так колебания туда-обратно
(4200 → 4300 → 4200) не засыпают телефон уведомлениями.
"""
from __future__ import annotations

import re

from dozor.config import Notify
from dozor.util import clock, clock_msk, fmt_short_date, norm_train, pct_str, rub, seats_word, signed, title_case


def train_matches(flt: list[str], number: str) -> bool:
    if not flt:
        return True
    n = norm_train(number)
    return any(norm_train(f) == n for f in flt)


def significant(old: float | None, new: float | None, notify: Notify) -> bool:
    """Изменение цены достаточно велико: сработали оба порога, в рублях и в процентах."""
    if old is None or new is None or old <= 0:
        return False
    d = abs(new - old)
    return d >= max(notify.price_min_rub, 1) and d / old * 100 >= notify.price_min_pct


def _groups_text(groups: list[dict]) -> str:
    return " · ".join(
        f"{g['label']} — {'места есть' if g.get('approx') else seats_word(g['seats'])}"
        + (f" от {rub(g['min_price'])}" if g.get("min_price") is not None else "")
        for g in groups
    )


def _total_text(t: dict) -> str:
    return f"не менее {seats_word(t['seats'])}" if any(g.get("approx") for g in t["groups"]) else seats_word(t["seats"])


def _clear_baselines(base: dict, number: str) -> None:
    for k in [k for k in base if k.startswith(f"{number}|")]:
        del base[k]


def diff_trains(*, watch: str, prev: dict | None, nxt: dict, baselines: dict, known: list[str], notify: Notify, now: int, names: dict, stats=None) -> dict:
    stats = stats or (lambda number, label: None)
    events: list[dict] = []
    base = {k: dict(v) for k, v in baselines.items()}
    known_set = list(known)
    first = prev is None
    prev_by = {t["number"]: t for t in (prev or {}).get("trains", [])}
    next_nums = {t["number"] for t in nxt["trains"]}
    gap = notify.price_gap_min * 60
    short = fmt_short_date(nxt["date"])

    for t in nxt["trains"]:
        p = prev_by.get(t["number"])
        is_new = not first and p is None and t["number"] not in known_set
        if t["number"] not in known_set:
            known_set.append(t["number"])
        prev_seats = p["seats"] if p else 0
        num = t.get("display_number") or t["number"]
        tag = f"avail:{watch}:{t['number']}"
        src, dst = names.get("from") or title_case(t.get("from")), names.get("to") or title_case(t.get("to"))
        where = " ".join(f"{src} {clock(t.get('depart_local'))} → {dst} {clock(t.get('arrive_local'))}".split())

        def bkey(g: dict, t=t) -> str:
            return f"{t['number']}|{g['key']}"

        if prev_seats == 0 and t["seats"] > 0:
            for g in t["groups"]:
                base[bkey(g)] = {"price": g["min_price"], "at": now}
            events.append({
                "kind": "tickets_appeared", "urgent": True, "buy": True, "watch": watch, "entity": t["number"], "tag": tag,
                "title": f"{'🆕 Новый поезд с билетами' if is_new else '🚆 Появились билеты'} {num} · {short}",
                "body": f"{_groups_text(t['groups'])}. Всего {_total_text(t)}. {where}.",
            })
            continue

        if prev_seats > 0 and t["seats"] == 0:
            _clear_baselines(base, t["number"])
            if notify.sold_out:
                events.append({
                    "kind": "sold_out", "urgent": False, "watch": watch, "entity": t["number"], "tag": tag,
                    "title": f"⛔ {num} · {short}: билеты закончились",
                    "body": f"Было {seats_word(prev_seats)}. Продолжаю следить — сообщу, когда появятся снова.",
                })
            continue

        if t["seats"] == 0:
            if is_new and notify.new_train:
                events.append({
                    "kind": "new_train", "urgent": False, "watch": watch, "entity": t["number"], "tag": f"new:{watch}:{t['number']}",
                    "title": f"🆕 Новый поезд на {short}: {num}",
                    "body": f"{where}. Билетов пока нет — слежу за ним.",
                })
            continue

        # Места были и остались: смотрим на новые классы вагонов и на цены.
        opened, changes = [], []
        prev_groups = {g["key"]: g for g in p["groups"]}
        for g in t["groups"]:
            k = bkey(g)
            pg = prev_groups.get(g["key"])
            if pg is None or pg["seats"] <= 0:
                opened.append(g)
                base[k] = {"price": g["min_price"], "at": now}
                continue
            b = base.get(k)
            if not b or b.get("price") is None:
                if g["min_price"] is not None:
                    base[k] = {"price": g["min_price"], "at": now}
                continue
            if g["min_price"] is not None and significant(b["price"], g["min_price"], notify) and now - b["at"] >= gap:
                changes.append({"g": g, "from": b["price"], "to": g["min_price"]})
                base[k] = {"price": g["min_price"], "at": now}
        for g in p["groups"]:
            if not any(x["key"] == g["key"] for x in t["groups"]):
                base.pop(f"{t['number']}|{g['key']}", None)

        if opened:
            events.append({
                "kind": "seats_appeared", "urgent": True, "buy": True, "watch": watch, "entity": t["number"], "tag": tag,
                "title": f"🚆 {num} · {short}: появились места — {', '.join(g['label'] for g in opened)}",
                "body": f"{_groups_text(opened)}. Всего {_total_text(t)}.",
            })
        if changes:
            downs = sum(1 for c in changes if c["to"] < c["from"])
            ups = len(changes) - downs
            icon = "📉" if ups == 0 else "📈" if downs == 0 else "↕️"
            verb = "цена снизилась" if ups == 0 else "цена выросла" if downs == 0 else "цены изменились"
            parts = [f"{c['g']['label']}: {rub(c['from'])} → {rub(c['to'])} ({signed(c['to'] - c['from'])} ₽, {pct_str(c['from'], c['to'])})" for c in changes]
            biggest = max(changes, key=lambda c: abs(c["to"] - c["from"]))
            st = stats(t["number"], biggest["g"]["label"])
            dyn = f" За время наблюдения: мин {rub(st['min'])}, макс {rub(st['max'])}." if st else ""
            events.append({
                "kind": "price_changed", "urgent": False, "buy": True, "watch": watch, "entity": t["number"], "tag": f"price:{watch}:{t['number']}",
                "title": f"{icon} {num} · {short}: {verb}",
                "body": f"{'; '.join(parts)}. Мест: {t['seats']}.{dyn}",
                "data": {"changes": [{"label": c["g"]["label"], "from": c["from"], "to": c["to"]} for c in changes]},
            })

    # Поезд пропал из выдачи (а выдача полная) — для нас это то же самое, что «билеты закончились».
    if not nxt.get("partial"):
        for p in (prev or {}).get("trains", []):
            if p["number"] in next_nums or p["seats"] <= 0:
                continue
            _clear_baselines(base, p["number"])
            if notify.sold_out:
                events.append({
                    "kind": "sold_out", "urgent": False, "watch": watch, "entity": p["number"], "tag": f"avail:{watch}:{p['number']}",
                    "title": f"⛔ {p.get('display_number') or p['number']} · {short}: пропал из выдачи",
                    "body": f"Раньше было {seats_word(p['seats'])}. Продолжаю следить.",
                })
    return {"events": events, "baselines": base, "known": known_set}


def _offer_text(o: dict) -> str:
    head = " ".join(x for x in (o.get("airline_name") or o.get("airline"), o.get("flight_number")) if x)
    return re.sub(r" {2,}", " ", f"{head}, вылет {clock_msk(o.get('departure_at'))} — {rub(o['price'])}").strip()


def diff_flights(*, watch: str, prev: dict | None, nxt: dict, baselines: dict, known: list[str], notify: Notify, now: int, names: dict, stats=None) -> dict:
    """prev/nxt — «эффективные» снимки: монитор удерживает предложение ещё 6 часов после исчезновения из кэша Aviasales."""
    stats = stats or (lambda: None)
    events: list[dict] = []
    base = {k: dict(v) for k, v in baselines.items()}
    known_set = list(known)
    gap = notify.price_gap_min * 60
    short = fmt_short_date(nxt["date"])
    route = f"{names.get('origin', '')} → {names.get('destination', '')}".strip()
    prev_keys = {o["key"] for o in (prev or {}).get("offers", [])}
    had_any = bool((prev or {}).get("offers"))
    has_any = bool(nxt["offers"])
    cheapest = nxt["offers"][0] if has_any else None

    def remember(offers: list[dict]) -> None:
        for o in offers:
            if o["key"] not in known_set:
                known_set.append(o["key"])

    if not had_any and has_any:
        base["cheapest"] = {"price": cheapest["price"], "at": now}
        remember(nxt["offers"])
        more = f" и ещё {len(nxt['offers']) - 3}" if len(nxt["offers"]) > 3 else ""
        events.append({
            "kind": "flight_appeared", "urgent": True, "buy": True, "watch": watch, "tag": f"avail:{watch}",
            "title": f"✈️ Появились билеты {route} · {short}",
            "body": f"{'; '.join(_offer_text(o) for o in nxt['offers'][:3])}{more}.",
        })
    elif had_any and not has_any:
        base.pop("cheapest", None)
    elif had_any and has_any:
        fresh = [o for o in nxt["offers"] if o["key"] not in prev_keys and o["key"] not in known_set]
        remember(nxt["offers"])
        if fresh and notify.new_train:
            events.append({
                "kind": "new_flight", "urgent": False, "buy": True, "watch": watch, "tag": f"new:{watch}",
                "title": f"🆕 Новый рейс {route} · {short}",
                "body": f"{'; '.join(_offer_text(o) for o in fresh)}.",
            })
        b = base.get("cheapest")
        if not b or b.get("price") is None:
            base["cheapest"] = {"price": cheapest["price"], "at": now}
        elif significant(b["price"], cheapest["price"], notify) and now - b["at"] >= gap:
            down = cheapest["price"] < b["price"]
            st = stats()
            dyn = f" За время наблюдения: мин {rub(st['min'])}, макс {rub(st['max'])}." if st else ""
            events.append({
                "kind": "price_changed", "urgent": False, "buy": True, "watch": watch, "tag": f"price:{watch}",
                "title": f"{'📉' if down else '📈'} {route} · {short}: {'цена снизилась' if down else 'цена выросла'}",
                "body": f"Самый дешёвый: {rub(b['price'])} → {rub(cheapest['price'])} ({signed(cheapest['price'] - b['price'])} ₽, {pct_str(b['price'], cheapest['price'])}). {_offer_text(cheapest)}.{dyn}",
                "data": {"from": b["price"], "to": cheapest["price"]},
            })
            base["cheapest"] = {"price": cheapest["price"], "at": now}
    return {"events": events, "baselines": base, "known": known_set}
