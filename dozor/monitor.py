"""Ядро: за один запуск опрашивает источники, сравнивает со знанием о прошлом, пишет историю цен
и возвращает события для уведомлений. Время и источники подставляются снаружи, поэтому логика
проверяется тестами без сети.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
from dataclasses import dataclass

from dozor import sources as registry
from dozor.config import Config
from dozor.diff import diff_flights, diff_trains, train_matches
from dozor.links import flight_links, train_links
from dozor.util import (
    SourceError, fmt_date, fmt_short_date, iata_name, msk, rub, seats_word, signed, stamp, title_case, today_msk,
)

log = logging.getLogger(__name__)

MIN = 60
HOUR = 3600
DAY = 86400
FLIGHT_GRACE = 6 * HOUR   # сколько держим рейс в списке после исчезновения из кэша Aviasales
HEARTBEAT = 3 * HOUR      # точка в историю — минимум раз в 3 часа, даже если ничего не менялось
SAVE_BEAT = 30 * MIN      # как часто «подтверждать» неизменившиеся данные записью (иначе коммит не создаётся)
FLIGHT_SLACK = 4 * MIN    # запас, чтобы проверка раз в 30 минут не сдвигалась на следующий запуск


@dataclass(frozen=True)
class Watch:
    key: str
    type: str   # "train" | "flight"
    date: str

    @property
    def prefix(self) -> str:   # «train», «train2»… или «flight»
        return self.key.split(":", 1)[0]


def new_meta() -> dict:
    return {
        "baselines": {},          # сообщённые цены
        "known": [],              # поезда/рейсы, которые уже видели
        "health": {},             # источник → {since, notified_at, message, kind}
        "first_available_at": None,
        "available_since": None,
        "offers_seen": {},        # рейсы: key → {offer, at}
    }


def _content(snapshot: dict) -> str:
    return json.dumps({k: v for k, v in snapshot.items() if k != "at"}, sort_keys=True, ensure_ascii=False)


class Monitor:
    def __init__(self, cfg: Config, state: dict, history: dict, env: dict, now: int, *, session=None, fetchers: dict | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.history = history
        self.env = env
        self.now = now
        self.session = session
        self.fetchers = fetchers or {}   # {(kind, name): fetch(cfg, date, ctx)} — для тестов
        self.events: list[dict] = []

    # ───────────────────────── что наблюдаем ─────────────────────────

    def defs(self) -> list[Watch]:
        today = today_msk(self.now)
        out = []
        for prefix, route in self.cfg.train_routes():
            if route.enabled:
                out += [Watch(f"{prefix}:{d}", "train", d) for d in route.dates if d >= today]
        if self.cfg.flights.enabled:
            out += [Watch(f"flight:{d}", "flight", d) for d in self.cfg.flights.dates if d >= today]
        return out

    def ws(self, key: str) -> dict:
        w = self.state["watches"].setdefault(key, {"good": None, "last": None, "meta": new_meta()})
        w.setdefault("meta", new_meta())
        return w

    def forget_unused(self) -> None:
        keys = {d.key for d in self.defs()}
        for key in [k for k in self.state["watches"] if k not in keys]:
            del self.state["watches"][key]

    def route(self, prefix: str):
        """Настройки маршрута поезда по префиксу ключа («train», «train2»…)."""
        return dict(self.cfg.train_routes()).get(prefix, self.cfg.train)

    def sources_for(self, w: Watch) -> list[str]:
        return self.route(w.prefix).sources if w.type == "train" else self.cfg.flights.sources

    def route_name(self, w: Watch) -> str:
        r = self.route(w.prefix)
        return f"{title_case(r.from_name)} → {title_case(r.to_name)}"

    # ───────────────────────── один запуск ─────────────────────────

    def run(self, *, force: bool = False) -> list[dict]:
        self.forget_unused()
        self._reset_if_settings_changed()
        for w in self.defs():
            ws = self.ws(w.key)
            if w.type == "flight" and not force:
                tried = ws.get("tried_at")
                if tried and self.now - tried < self.cfg.flights.interval_min * MIN - FLIGHT_SLACK:
                    continue
            self.check(w)
        return self.events

    def _fingerprint(self) -> dict:
        f = self.cfg.flights
        out = {prefix: [t.from_name, t.to_name, t.from_code, t.to_code, sorted(t.trains), sorted(t.sources)] for prefix, t in self.cfg.train_routes()}
        out["flight"] = [f.origin, f.destination, f.direct_only, f.currency, sorted(f.sources)]
        return out

    def _reset_if_settings_changed(self) -> None:
        """Сменили маршрут или фильтр — старые снимки и «сообщённые цены» больше не годятся."""
        fp, old = self._fingerprint(), self.state.get("fingerprint") or {}
        for kind in fp:
            if kind in old and old[kind] != fp[kind]:
                for key in [k for k in self.state["watches"] if k.startswith(f"{kind}:")]:
                    self.state["watches"][key] = {"good": None, "last": None, "meta": new_meta()}
                log.info("Настройки (%s) изменились — начинаю наблюдение заново", kind)
        self.state["fingerprint"] = fp

    def check(self, w: Watch) -> None:
        ws = self.ws(w.key)
        if w.type == "flight":
            ws["tried_at"] = self.now   # у поезда отметка не нужна: он проверяется каждый запуск (а запись в state — это лишний коммит)
        results, errors = self._gather(w)
        self._update_health(w, ws, results, errors)
        if not results:
            first = next(iter(errors.values()))
            self._on_failure(w, ws, first)
            return
        snapshot = self._merge(w, ws, results)
        self._on_success(w, ws, snapshot)

    # ───────────────────────── опрос источников ─────────────────────────

    def _fetch_one(self, w: Watch, name: str) -> dict:
        module = registry.get(w.type, name)
        problem = module.check_env(self.env)
        if problem:
            raise SourceError(problem, "config")
        fetch = self.fetchers.get((w.type, name), module.fetch)
        cfg = dataclasses.replace(self.cfg, train=self.route(w.prefix)) if w.type == "train" else self.cfg   # источник поезда читает cfg.train
        return fetch(cfg, w.date, {"env": self.env, "state": self.state, "now": self.now, "session": self.session})

    def _gather(self, w: Watch) -> tuple[dict, dict]:
        results, errors = {}, {}
        for name in self.sources_for(w):
            try:
                results[name] = self._fetch_one(w, name)
                self._record_source(name, ok=results[name])
            except SourceError as exc:
                errors[name] = exc
                self._record_source(name, error=exc)
            except Exception as exc:  # ошибка в коде разбора не должна ронять остальные проверки
                log.exception("Сбой источника %s", name)
                errors[name] = SourceError(f"Внутренняя ошибка разбора: {type(exc).__name__}: {exc}", "internal")
                self._record_source(name, error=errors[name])
        return results, errors

    def _record_source(self, name: str, *, ok: dict | None = None, error: SourceError | None = None) -> None:
        src = self.state["sources"].setdefault(name, {})
        if ok is not None:
            prev = src.get("last_ok")
            fresh = prev is None or self.now - prev["at"] >= SAVE_BEAT or src.get("last_error", {}).get("at", 0) > prev["at"]
            if fresh:
                meta = ok.get("meta") or {}
                src["last_ok"] = {"at": self.now, "status": meta.get("status"), "ms": meta.get("ms")}
                if ok.get("excerpt"):
                    src["excerpt"] = ok["excerpt"][:1200]
        if error is not None:
            prev = src.get("last_error")
            if not prev or prev["message"] != str(error) or self.now - prev["at"] >= SAVE_BEAT:
                src["last_error"] = {"at": self.now, "message": str(error), "kind": error.kind, "excerpt": str(error.meta.get("excerpt", ""))[:1200]}

    def _merge(self, w: Watch, ws: dict, results: dict) -> dict:
        if w.type == "train":
            trains, seen = [], set()
            route, partial = None, False
            for res in results.values():
                route = route or res.get("route")
                partial = partial or bool(res.get("partial"))
                for t in res["trains"]:
                    if t["number"] not in seen:
                        seen.add(t["number"])
                        trains.append(t)
            if partial and ws.get("good"):
                # выдача неполная: поезд мог просто не попасть в ответ — помним его по прошлой проверке
                for old in ws["good"]["snapshot"].get("trains", []):
                    if old["number"] not in seen:
                        seen.add(old["number"])
                        trains.append(old)
            trains.sort(key=lambda t: str(t.get("depart_local")))
            return {
                "type": "train", "date": w.date, "at": self.now, "partial": partial, "route": route,
                "all_numbers": [t["number"] for t in trains],
                "trains": [t for t in trains if train_matches(self.route(w.prefix).trains, t["number"])],
            }
        # рейсы: предложение «липнет» на 6 часов — кэш Aviasales мигает, и ложные «появились/пропали» ни к чему
        seen_offers = ws["meta"]["offers_seen"]
        fresh_keys = set()
        for name, res in results.items():
            for o in res["offers"]:
                cur = seen_offers.get(o["key"])
                if o["key"] in fresh_keys and cur and cur["offer"]["price"] <= o["price"]:
                    continue  # этот рейс уже нашёл другой источник дешевле или так же
                seen_offers[o["key"]] = {"offer": {**o, "source": name}, "at": self.now}
                fresh_keys.add(o["key"])
        for key in [k for k, v in seen_offers.items() if self.now - v["at"] > FLIGHT_GRACE]:
            del seen_offers[key]
        offers = sorted(
            ({**v["offer"], "stale": k not in fresh_keys} for k, v in seen_offers.items()),
            key=lambda o: o["price"],
        )
        return {"type": "flight", "date": w.date, "at": self.now, "offers": offers}

    # ───────────────────────── итоги проверки ─────────────────────────

    def _update_health(self, w: Watch, ws: dict, results: dict, errors: dict) -> None:
        health = ws["meta"]["health"]
        title = {"rzd": "РЖД", "travelpayouts": "Aviasales"}
        for name in results:
            h = health.pop(name, None)
            if h and h.get("notified_at"):
                self._event({
                    "kind": "source_recovered", "urgent": False, "watch": w.key, "tag": f"health:{w.key}:{name}",
                    "title": f"✅ {title.get(name, name)} снова отвечает",
                    "body": f"Проверка {fmt_date(w.date)} работает в штатном режиме.",
                })
        for name, exc in errors.items():
            h = health.setdefault(name, {"since": self.now, "notified_at": None})
            h["message"], h["kind"] = str(exc), exc.kind
            minutes = (self.now - h["since"]) / MIN
            due = not h.get("notified_at") or self.now - h["notified_at"] > DAY
            if exc.kind != "config" and minutes >= self.cfg.notify.error_after_min and due:
                h["notified_at"] = self.now
                what = "билеты РЖД" if w.type == "train" else "рейсы"
                self._event({
                    "kind": "source_error", "urgent": False, "watch": w.key, "tag": f"health:{w.key}:{name}",
                    "title": f"⚠️ Не могу проверить {what} {fmt_short_date(w.date)}",
                    "body": f"{title.get(name, name)}: {exc}. Не получается уже {int(minutes)} мин — пока это не исправлено, уведомления о билетах не придут.",
                })

    def _on_failure(self, w: Watch, ws: dict, exc: SourceError) -> None:
        log.warning("%s: %s", w.key, exc)
        last = ws.get("last")
        if not last or last["ok"] or last.get("error") != str(exc) or self.now - last["at"] >= SAVE_BEAT:
            ws["last"] = {"ok": False, "at": self.now, "kind": exc.kind, "error": str(exc)}

    def _on_success(self, w: Watch, ws: dict, snapshot: dict) -> None:
        prev = ws["good"]["snapshot"] if ws.get("good") else None
        changed = prev is None or _content(prev) != _content(snapshot)
        self._append_history(w, snapshot)

        names = (
            {"from": title_case(self.route(w.prefix).from_name), "to": title_case(self.route(w.prefix).to_name)}
            if w.type == "train"
            else {"origin": iata_name(self.cfg.flights.origin), "destination": iata_name(self.cfg.flights.destination)}
        )
        args = dict(watch=w.key, prev=prev, nxt=snapshot, baselines=ws["meta"]["baselines"], known=ws["meta"]["known"], notify=self.cfg.notify, now=self.now, names=names)
        if w.type == "train":
            result = diff_trains(**args, stats=lambda number, label: self._group_stats(w, number, label))
        else:
            result = diff_flights(**args, stats=lambda: self._flight_stats(w))
        ws["meta"]["baselines"], ws["meta"]["known"] = result["baselines"], result["known"]
        for ev in result["events"]:
            self._event(ev)

        available = any(t["seats"] > 0 for t in snapshot["trains"]) if w.type == "train" else bool(snapshot["offers"])
        if available:
            ws["meta"]["available_since"] = ws["meta"]["available_since"] or self.now
            ws["meta"]["first_available_at"] = ws["meta"]["first_available_at"] or self.now
        else:
            ws["meta"]["available_since"] = None

        if changed or not ws.get("good"):
            ws["good"] = {"at": self.now, "snapshot": snapshot}
        elif self.now - ws["good"]["at"] >= SAVE_BEAT:
            ws["good"]["at"] = self.now  # данные те же — подтверждаем раз в полчаса, чтобы не плодить коммиты
        last = ws.get("last")
        if not last or not last["ok"] or self.now - last["at"] >= SAVE_BEAT:
            ws["last"] = {"ok": True, "at": self.now}

    def _event(self, ev: dict) -> None:
        prefix = ev["watch"].split(":", 1)[0]
        if prefix.startswith("train") and prefix != "train":   # дополнительный маршрут: в заголовке видно, какой
            r = self.route(prefix)
            ev["title"] += f" · {title_case(r.from_name)} → {title_case(r.to_name)}"
        log.info("[событие] %s %s: %s", ev["kind"], ev["watch"], ev["title"])
        self.events.append(ev)

    # ───────────────────────── история цен ─────────────────────────

    def _append_history(self, w: Watch, snapshot: dict) -> None:
        series = self.history["series"]

        def push(key: str, label: str, point: dict, same) -> None:
            ser = series.setdefault(key, {"type": w.type, "watch": w.key, "date": w.date, "label": label, "points": []})
            ser["label"] = label
            last = ser["points"][-1] if ser["points"] else None
            if last and same(last, point) and self.now - last["t"] < HEARTBEAT:
                return
            ser["points"].append(point)

        if w.type == "train":
            for t in snapshot["trains"]:
                push(f"{w.prefix}|{w.date}|{t['number']}", t.get("display_number") or t["number"],
                     {"t": self.now, "s": t["seats"], "p": t["min_price"], "g": {g["label"]: [g["seats"], g["min_price"]] for g in t["groups"]}},
                     lambda a, b: a["s"] == b["s"] and a["p"] == b["p"] and a["g"] == b["g"])
        else:
            cheapest = snapshot["offers"][0] if snapshot["offers"] else None
            label = f"{iata_name(self.cfg.flights.origin)} → {iata_name(self.cfg.flights.destination)}"
            push(f"flight|{w.date}", label,
                 {"t": self.now, "c": len(snapshot["offers"]), "p": cheapest["price"] if cheapest else None, "f": cheapest["flight_number"] if cheapest else None},
                 lambda a, b: a["c"] == b["c"] and a["p"] == b["p"] and a["f"] == b["f"])

    def _group_stats(self, w: Watch, number: str, label: str) -> dict | None:
        ser = self.history["series"].get(f"{w.prefix}|{w.date}|{number}")
        prices = [pt["g"][label][1] for pt in (ser["points"] if ser else []) if label in pt.get("g", {}) and pt["g"][label][1] is not None]
        return {"min": min(prices), "max": max(prices)} if prices else None

    def _flight_stats(self, w: Watch) -> dict | None:
        ser = self.history["series"].get(f"flight|{w.date}")
        prices = [pt["p"] for pt in (ser["points"] if ser else []) if pt.get("p") is not None]
        return {"min": min(prices), "max": max(prices)} if prices else None

    def _ref_price(self, w: Watch, since: int) -> dict | None:
        """Эталон для «как менялась цена»: последняя точка не позже `since`, а если история короче — самая ранняя."""
        best = None
        for ser in self.history["series"].values():
            if ser["watch"] != w.key:
                continue
            pts = ser["points"]
            i = len(pts) - 1
            while i >= 0 and pts[i]["t"] > since:
                i -= 1
            ref = next((pts[j] for j in range(i, -1, -1) if pts[j]["p"] is not None), None)
            if ref is None:
                ref = next((pts[j] for j in range(i + 1, len(pts)) if pts[j]["p"] is not None), None)
            if ref and (best is None or ref["p"] < best["price"]):
                best = {"price": ref["p"], "t": ref["t"]}
        return best

    # ───────────────────────── ссылки, сводка, модель для приложения ─────────────────────────

    def links(self, key: str) -> list[dict]:
        prefix, date = key.split(":", 1)
        if prefix.startswith("train"):
            route = ((self.state["watches"].get(key) or {}).get("good") or {}).get("snapshot", {}).get("route")
            return train_links(dataclasses.replace(self.cfg, train=self.route(prefix)), route, date)
        return flight_links(self.cfg, date)

    def summary_of(self, w: Watch) -> dict:
        ws = self.ws(w.key)
        snap = ws["good"]["snapshot"] if ws.get("good") else None
        last = ws.get("last")
        status = "pending" if not last else "ok" if last["ok"] else "config" if last.get("kind") == "config" else "error"
        c = self.cfg
        base = {
            "key": w.key, "type": w.type, "date": w.date, "status": status,
            "last_ok_at": ws["good"]["at"] if ws.get("good") else None,
            "error": last.get("error") if last and not last["ok"] else None,
            "error_kind": last.get("kind") if last and not last["ok"] else None,
            "first_available_at": ws["meta"]["first_available_at"],
            "available_since": ws["meta"]["available_since"],
            "sources": self.sources_for(w),
            "links": self.links(w.key),
        }
        if w.type == "train":
            trains = snap["trains"] if snap else []
            total = sum(t["seats"] for t in trains)
            prices = [t["min_price"] for t in trains if t["min_price"] is not None]
            r = self.route(w.prefix)
            base.update({
                "title": "Поезд", "subtitle": f"{self.route_name(w)}, прямой", "label": r.label,
                "trains": trains, "seats_total": total, "available": total > 0, "min_price": min(prices) if prices else None,
                "partial": bool(snap and snap.get("partial")), "all_numbers": snap.get("all_numbers", []) if snap else [],
                "watched_trains": r.trains,
            })
        else:
            offers = snap["offers"] if snap else []
            base.update({
                "title": "Самолёт", "subtitle": f"{iata_name(c.flights.origin)} → {iata_name(c.flights.destination)}, {'прямой' if c.flights.direct_only else 'любой'}",
                "offers": offers, "available": bool(offers), "min_price": offers[0]["price"] if offers else None,
            })
        ref = self._ref_price(w, self.now - DAY) if base["min_price"] is not None else None
        base["change24h"] = {"from": ref["price"], "to": base["min_price"], "delta": base["min_price"] - ref["price"], "since": ref["t"]} if ref else None
        return base

    def summaries(self) -> list[dict]:
        return [self.summary_of(w) for w in self.defs()]

    def digest_line(self, s: dict) -> str:
        icon = "🚆" if s["type"] == "train" else "✈️"
        head = f"{icon} {fmt_short_date(s['date'])}"
        if s["type"] == "train" and s["key"].split(":", 1)[0] != "train":   # дополнительный маршрут — с названием
            head += f" {s['subtitle'].removesuffix(', прямой')}"
        if s["status"] == "config":
            return f"{head}: {s['error']}"
        if s["status"] == "pending":
            return f"{head}: ещё не проверял"
        if not s["last_ok_at"]:
            return f"{head}: нет данных — {s['error']}"
        note = f" (сейчас ошибка источника: {s['error']})" if s["status"] == "error" else ""
        if not s["available"]:
            return f"{head}: билетов нет{' в выдаче Aviasales' if s['type'] == 'flight' else ''}, проверено {stamp(s['last_ok_at'])}{note}"
        what = seats_word(s["seats_total"]) if s["type"] == "train" else f"{len(s['offers'])} вариант(ов)"
        c = s["change24h"]
        dyn = ""
        if c and c["delta"] != 0:
            dyn = f", {'за сутки' if self.now - c['since'] >= 20 * HOUR else 'с ' + stamp(c['since'])} {signed(c['delta'])} ₽"
        price = f", от {rub(s['min_price'])}{dyn}" if s["min_price"] is not None else ""
        return f"{head}: есть {what}{price}{note}"

    def digest_body(self) -> str:
        return "\n".join(self.digest_line(s) for s in self.summaries())

    def maybe_digest(self, has_targets: bool) -> dict | None:
        hour = self.cfg.notify.digest_hour
        if hour is None or not has_targets:
            return None
        today = today_msk(self.now)
        if self.state.get("digest_day") == today or msk(self.now).hour < hour:
            return None
        if not any(self.ws(d.key).get("good") for d in self.defs()):
            return None  # ещё нечего сообщать
        self.state["digest_day"] = today
        first = self.defs()[0].key if self.defs() else "train"
        return {"kind": "digest", "urgent": False, "watch": first, "tag": "digest", "title": "📋 Сводка: слежу за билетами", "body": self.digest_body()}

    def sources_summary(self) -> dict:
        out = {}
        for name, mod in list(registry.TRAIN.items()) + list(registry.FLIGHT.items()):
            s = self.state["sources"].get(name, {})
            out[name] = {
                "title": mod.TITLE, "kind": mod.KIND, "configured": mod.check_env(self.env) is None,
                "enabled": any(name in (t.sources if mod.KIND == "train" else self.cfg.flights.sources) for _, t in self.cfg.train_routes()),
                "last_ok": copy.deepcopy(s.get("last_ok")), "last_error": copy.deepcopy(s.get("last_error")),
                "excerpt": s.get("excerpt"),
            }
        return out
