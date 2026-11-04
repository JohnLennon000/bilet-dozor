import pytest

from dozor.monitor import Monitor
from dozor.sources import rzd, travelpayouts
from dozor.util import SourceError, parse_iso
from tests.helpers import group, make_cfg, offer, rzd_response, tp_response, train

T0 = parse_iso("2026-11-03T09:00:00Z")


class World:
    """Что «отвечают» источники сейчас. Меняется между запусками."""

    def __init__(self):
        self.rzd = rzd_response([train("301У", [])])
        self.tp = tp_response([])
        self.rzd_error = None
        self.tp_error = None

    def fetchers(self):
        def rzd_fetch(cfg, date, ctx):
            if self.rzd_error:
                raise self.rzd_error
            p = rzd.parse_train_pricing(self.rzd)
            return {"trains": p["trains"], "partial": p["partial"], "route": {"from": {"code": "2024750"}, "to": {"code": "2000000"}}, "meta": {"status": 200, "ms": 5}, "excerpt": ""}

        def tp_fetch(cfg, date, ctx):
            if self.tp_error:
                raise self.tp_error
            return {"offers": travelpayouts.parse_flights(self.tp), "meta": {"status": 200, "ms": 5}, "excerpt": ""}
        return {("train", "rzd"): rzd_fetch, ("flight", "travelpayouts"): tp_fetch}


class Rig:
    def __init__(self, cfg=None, env=None):
        self.cfg = cfg or make_cfg()
        self.env = {"TRAVELPAYOUTS_TOKEN": "t"} if env is None else env
        self.state = {"watches": {}, "sources": {}, "events": []}
        self.history = {"series": {}}
        self.world = World()
        self.now = T0

    def run(self, minutes=10, **kw):
        self.now += minutes * 60
        mon = Monitor(self.cfg, self.state, self.history, self.env, self.now, fetchers=self.world.fetchers())
        mon.run(**kw)
        return mon, mon.events

    def kinds(self, events, watch=None):
        return [(e["kind"], e["watch"]) for e in events if watch is None or e["watch"] == watch]


def test_first_run_is_silent_when_nothing_is_on_sale():
    rig = Rig()
    _, ev = rig.run()
    assert ev == []


def test_sold_out_301u_then_tickets_appear_is_urgent_with_buy_flag():
    rig = Rig()
    rig.run()
    rig.world.rzd = rzd_response([train("301У", [group("ReservedSeat", 6, 2300), group("Compartment", 2, 4200)])])
    _, ev = rig.run()
    [e] = [x for x in ev if x["watch"] == "train:2027-01-09"]
    assert e["kind"] == "tickets_appeared" and e["urgent"] and e["buy"]
    assert "301У" in e["title"] and "09.01" in e["title"]
    assert "Плацкарт — 6 мест от 2" in e["body"] and "Купе — 2 места" in e["body"]


def test_extra_train_for_holidays_is_reported_as_new_train_with_tickets():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [])])
    rig.run()
    rig.world.rzd = rzd_response([train("301У", []), train("303Я", [group("Compartment", 20, 5100)])])
    _, ev = rig.run()
    [e] = ev
    assert e["kind"] == "tickets_appeared" and "Новый поезд" in e["title"] and "303Я" in e["title"]


def test_extra_train_without_tickets_is_non_urgent_notice():
    rig = Rig()
    rig.run()
    rig.world.rzd = rzd_response([train("301У", []), train("303Я", [])])
    _, ev = rig.run()
    assert [(e["kind"], e["urgent"]) for e in ev] == [("new_train", False)]


def test_added_cars_on_a_train_that_already_had_seats():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200), group("ReservedSeat", 30, 2300)])])
    _, ev = rig.run()
    assert [(e["kind"], e["urgent"]) for e in ev] == [("seats_appeared", True)]


def test_price_dynamics_compared_with_last_reported_not_last_check():
    rig = Rig(make_cfg(notify={"price_gap_min": 0, "price_min_rub": 100, "price_min_pct": 1}))
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, 4000)])])
    rig.run()
    for price in (4040, 4080):   # медленный рост по 40 ₽ — порог 100 ₽ ещё не набран
        rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, price)])])
        _, ev = rig.run()
        assert ev == []
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, 4120)])])
    _, ev = rig.run()
    [e] = ev
    assert e["kind"] == "price_changed" and "4 000 ₽ → 4 120 ₽" in e["body"] and "+120" in e["body"]
    assert "За время наблюдения: мин" in e["body"]


def test_price_wobble_back_and_forth_is_quiet_after_first_report():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, 4200)])])
    rig.run()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, 4300)])])
    assert len(rig.run()[1]) == 1
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 5, 4300)])])
    assert rig.run()[1] == []


def test_sold_out_after_having_tickets():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    rig.world.rzd = rzd_response([train("301У", [])])
    _, ev = rig.run()
    assert [e["kind"] for e in ev] == ["sold_out"]
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 1, 4500)])])
    _, ev = rig.run()
    assert [(e["kind"], e["urgent"]) for e in ev] == [("tickets_appeared", True)]


def test_train_disappearing_from_complete_listing_counts_as_sold_out_but_not_if_partial():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    rig.world.rzd = rzd_response([], NotAllTrainsReturned=True)
    assert rig.run()[1] == []
    rig.world.rzd = rzd_response([])
    assert [e["kind"] for e in rig.run()[1]] == ["sold_out"]


def test_train_number_filter():
    rig = Rig(make_cfg(train={"trains": ["301y"]}))   # латиница вместо кириллицы — тот же поезд
    rig.world.rzd = rzd_response([train("301У", []), train("303Я", [])])
    rig.run()
    rig.world.rzd = rzd_response([train("301У", []), train("303Я", [group("Compartment", 9, 5000)])])
    assert rig.run()[1] == []
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 9, 5000)]), train("303Я", [])])
    assert [e["kind"] for e in rig.run()[1]] == ["tickets_appeared"]


def test_flights_checked_on_their_own_interval():
    rig = Rig()
    mon, _ = rig.run(minutes=10)
    assert "flight:2027-01-09" in rig.state["watches"] and rig.state["watches"]["flight:2027-01-09"]["tried_at"]
    t1 = rig.state["watches"]["flight:2027-01-09"]["tried_at"]
    rig.run(minutes=10)
    assert rig.state["watches"]["flight:2027-01-09"]["tried_at"] == t1      # рано
    rig.run(minutes=10)
    assert rig.state["watches"]["flight:2027-01-09"]["tried_at"] == t1      # ещё рано
    rig.run(minutes=10)                                                      # 30 минут
    assert rig.state["watches"]["flight:2027-01-09"]["tried_at"] > t1


def test_flight_appears_then_price_changes_and_cache_flicker_is_ignored():
    rig = Rig()
    rig.run()
    rig.world.tp = tp_response([offer("101", 6190)])
    _, ev = rig.run(minutes=30)
    flights = [e for e in ev if e["watch"].startswith("flight")]
    assert {e["watch"] for e in flights} == {"flight:2027-01-09", "flight:2027-01-10"}   # один и тот же ответ на обе даты
    assert all(e["kind"] == "flight_appeared" and e["urgent"] for e in flights)
    rig.world.tp = tp_response([])                      # кэш мигнул: рейса нет
    assert rig.run(minutes=30)[1] == []
    rig.world.tp = tp_response([offer("101", 6190)])    # и вернулся — не «новый рейс» и не «появились» снова
    assert rig.run(minutes=30)[1] == []
    rig.world.tp = tp_response([offer("101", 5490)])
    _, ev = rig.run(minutes=30)
    assert {e["kind"] for e in ev} == {"price_changed"} and "6 190 ₽ → 5 490 ₽" in ev[0]["body"]


def test_flight_really_gone_after_grace_then_reappears_is_urgent_again():
    rig = Rig()
    rig.world.tp = tp_response([offer("101", 6190)])
    rig.run()
    rig.world.tp = tp_response([])
    for _ in range(14):   # 7 часов без рейса в кэше
        rig.run(minutes=30)
    rig.world.tp = tp_response([offer("101", 6500)])
    _, ev = rig.run(minutes=30)
    assert any(e["kind"] == "flight_appeared" for e in ev)


def test_missing_token_is_config_status_not_silence_and_does_not_break_trains():
    rig = Rig(env={})
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    mon, ev = rig.run()
    assert [e["kind"] for e in ev] == ["tickets_appeared"]
    s = {x["key"]: x for x in mon.summaries()}
    assert s["flight:2027-01-09"]["status"] == "config" and "TRAVELPAYOUTS_TOKEN" in s["flight:2027-01-09"]["error"]
    assert s["train:2027-01-09"]["status"] == "ok"


def test_source_error_reported_after_threshold_once_then_recovery():
    rig = Rig()
    rig.world.rzd_error = SourceError("Источник закрыл доступ (HTTP 403)", "blocked")
    assert rig.run(minutes=30)[1] == []
    assert rig.run(minutes=30)[1] == []
    _, ev = rig.run(minutes=60)      # прошло больше 90 минут
    [e] = [x for x in ev if x["watch"].startswith("train")]
    assert e["kind"] == "source_error" and "HTTP 403" in e["body"] and "РЖД" in e["body"]
    assert [x for x in rig.run(minutes=30)[1] if x["watch"].startswith("train")] == []       # повторно не пишем
    rig.world.rzd_error = None
    _, ev = rig.run(minutes=30)
    assert [x["kind"] for x in ev if x["watch"].startswith("train")] == ["source_recovered"]


def test_error_status_keeps_last_good_data_visible():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    rig.world.rzd_error = SourceError("нет связи", "network")
    mon, _ = rig.run()
    s = {x["key"]: x for x in mon.summaries()}["train:2027-01-09"]
    assert s["status"] == "error" and s["available"] and s["seats_total"] == 2


def test_parser_crash_is_contained():
    rig = Rig()
    rig.world.rzd = {"Trains": "что-то совсем другое"}
    mon, ev = rig.run()
    s = {x["key"]: x for x in mon.summaries()}["train:2027-01-09"]
    assert s["status"] == "error" and "формат" in s["error"]


def test_changing_route_or_filter_resets_baselines():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    rig.cfg = make_cfg(train={"trains": ["303Я"]})
    _, ev = rig.run()
    assert ev == []   # у нового фильтра нет поездов — и не «закончились» из-за смены настроек
    rig.world.rzd = rzd_response([train("303Я", [group("Compartment", 2, 4200)])])
    assert [e["kind"] for e in rig.run()[1]] == ["tickets_appeared"]


def test_past_dates_are_dropped():
    rig = Rig(make_cfg(train={"dates": ["2026-10-01", "2027-01-09"]}))
    mon, _ = rig.run()
    assert [d.key for d in mon.defs() if d.type == "train"] == ["train:2027-01-09"]


def test_history_records_changes_and_heartbeat_not_every_run():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    for _ in range(5):
        rig.run()
    ser = rig.history["series"]["train|2027-01-09|301У"]
    assert len(ser["points"]) == 1
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4300)])])
    rig.run()
    assert [p["p"] for p in rig.history["series"]["train|2027-01-09|301У"]["points"]] == [4200, 4300]


def test_state_is_stable_between_identical_runs_so_no_commit_noise():
    import json

    def train_part(rig):
        w = {k: v for k, v in rig.state["watches"].items() if k.startswith("train")}
        return json.dumps({**rig.state, "watches": w, "events": []}, sort_keys=True)

    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    rig.run()
    before, hist = train_part(rig), json.dumps(rig.history["series"]["train|2027-01-09|301У"], sort_keys=True)
    rig.run(minutes=10)
    rig.run(minutes=10)
    assert train_part(rig) == before                 # состояние поезда не менялось — файл не перезапишется
    assert json.dumps(rig.history["series"]["train|2027-01-09|301У"], sort_keys=True) == hist


def test_digest_once_a_day_after_digest_hour():
    rig = Rig()
    rig.world.rzd = rzd_response([train("301У", [group("Compartment", 2, 4200)])])
    mon, _ = rig.run()     # 12:10 по Москве, час сводки 9
    d = mon.maybe_digest(True)
    assert d["kind"] == "digest" and "🚆 09.01: есть 2 места, от 4 200 ₽" in d["body"] and "✈️ 09.01: билетов нет в выдаче Aviasales" in d["body"]
    assert mon.maybe_digest(True) is None
    assert Monitor(rig.cfg, rig.state, rig.history, rig.env, rig.now + 3600).maybe_digest(True) is None   # в тот же день
    assert Monitor(rig.cfg, rig.state, rig.history, rig.env, rig.now + 86400).maybe_digest(True) is not None
    assert Monitor(make_cfg(), {"watches": {}, "sources": {}, "events": []}, {"series": {}}, {}, rig.now).maybe_digest(False) is None


# ───────────────────────── дополнительный маршрут поезда (extra_trains) ─────────────────────────

def _cfg_with_extra():
    from dozor.config import parse_config
    return parse_config({
        "train": {"dates": ["2027-01-09"], "from_code": "2024750", "to_code": "2000000"},
        "extra_trains": [{"from": "Москва", "to": "Уруссу", "dates": ["2026-12-25"], "label": "эксперимент"}],
        "flights": {"dates": ["2027-01-09"]},
        "notify": {"price_gap_min": 0},
    })


def test_extra_route_is_a_separate_watch_with_its_own_stations():
    rig = Rig(_cfg_with_extra())
    asked = []
    base = rig.world.fetchers()[("train", "rzd")]

    def spy(cfg, date, ctx):
        asked.append((cfg.train.from_name, cfg.train.to_name, date))
        return base(cfg, date, ctx)

    mon = Monitor(rig.cfg, rig.state, rig.history, rig.env, rig.now + 600, fetchers={**rig.world.fetchers(), ("train", "rzd"): spy})
    mon.run()
    assert ("Туймазы", "Москва", "2027-01-09") in asked and ("Москва", "Уруссу", "2026-12-25") in asked
    assert set(rig.state["watches"]) >= {"train:2027-01-09", "train2:2026-12-25"}
    s = {x["key"]: x for x in mon.summaries()}
    assert s["train2:2026-12-25"]["subtitle"] == "Москва → Уруссу, прямой" and s["train2:2026-12-25"]["label"] == "эксперимент"
    assert "Москва → Уруссу" in mon.digest_line(s["train2:2026-12-25"])


def test_extra_route_events_and_history_do_not_mix_with_main_route():
    rig = Rig(_cfg_with_extra())
    rig.run()
    rig.world.rzd = rzd_response([train("301У", [group("ReservedSeat", 4, 2500)])])
    _, ev = rig.run()
    titles = {e["watch"]: e["title"] for e in ev if e["kind"] == "tickets_appeared"}
    assert "Уруссу" in titles["train2:2026-12-25"] and "Уруссу" not in titles["train:2027-01-09"]
    assert "train|2027-01-09|301У" in rig.history["series"] and "train2|2026-12-25|301У" in rig.history["series"]
