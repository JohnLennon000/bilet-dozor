"""Полный проход main(): файлы данных, настоящее шифрование push, подписки, повторный запуск без изменений."""
import json
import shutil
from pathlib import Path

import pytest

from dozor import main as dozor_main
from dozor.push import Push
from dozor.webpush import device_id
from tests.helpers import PushService, group, offer, rzd_response, tp_response, train, write_fixture

ROOT = Path(__file__).parent.parent


@pytest.fixture
def project(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "push").mkdir()
    fixtures = tmp_path / "fx"
    write_fixture(fixtures, "rzd-2027-01-09.json", rzd_response([train("301У", [])]))
    monkeypatch.setenv("PUSH_SECRET", "test-secret")
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/bilet-dozor")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(dozor_main, "Push", lambda *a, **k: Push(*a, **k, allow_insecure=True))
    svc = PushService()
    (tmp_path / "push" / f"{device_id(svc.endpoint)}.json").write_text(json.dumps(svc.subscription()), encoding="utf-8")
    yield tmp_path, fixtures, svc
    svc.close()


def run(root, fixtures, now, *extra, force=True):
    return dozor_main.main(["--root", str(root), "--fixtures", str(fixtures), "--now", now, *(["--force"] if force else []), *extra])


def test_full_story_sold_out_to_tickets_to_price_change(project):
    root, fx, svc = project
    assert run(root, fx, "2026-11-03T09:00:00Z") == 0
    # новый телефон получил приветствие со сводкой, и это единственное сообщение
    assert [m["payload"]["kind"] for m in svc.messages] == ["welcome"]
    assert "09.01" in svc.messages[0]["payload"]["body"] and "билетов нет" in svc.messages[0]["payload"]["body"]
    summary = json.loads((root / "docs/data/summary.json").read_text(encoding="utf-8"))
    assert summary["push"]["devices"] == [device_id(svc.endpoint)] and len(summary["push"]["vapid"]) == 87
    assert summary["repo"] == "me/bilet-dozor"

    write_fixture(fx, "rzd-2027-01-09.json", rzd_response([train("301У", [group("Compartment", 3, 4200), group("ReservedSeat", 9, 2300)])]))
    run(root, fx, "2026-11-03T09:10:00Z")
    [m] = svc.messages[1:]
    p, h = m["payload"], m["headers"]
    assert p["kind"] == "tickets_appeared" and p["urgent"] and not p["silent"]
    assert "301У" in p["title"] and "Купе — 3 места" in p["body"]
    assert p["url"] == "./#w=train:2027-01-09"
    assert [l["label"] for l in p["links"]] == ["Купить на РЖД", "Яндекс Путешествия"]
    assert p["links"][0]["url"] == "https://ticket.rzd.ru/searchresults/v/1/2024750/2000000/2027-01-09"
    assert h["Urgency"] == "high" and h["Content-Encoding"] == "aes128gcm" and h["Authorization"].startswith("vapid t=")

    write_fixture(fx, "rzd-2027-01-09.json", rzd_response([train("301У", [group("Compartment", 3, 4500), group("ReservedSeat", 9, 2300)])]))
    run(root, fx, "2026-11-03T09:20:00Z")
    p = svc.messages[-1]["payload"]
    assert p["kind"] == "price_changed" and "4 200 ₽ → 4 500 ₽" in p["body"] and not p["urgent"]

    state = json.loads((root / "state/state.json").read_text(encoding="utf-8"))
    assert [e["kind"] for e in state["events"][:3]] == ["price_changed", "tickets_appeared"]
    assert state["events"][0]["delivery"]["push"] == {"ok": 1, "failed": 0}
    hist = json.loads((root / "docs/data/history.json").read_text(encoding="utf-8"))
    assert [pt["p"] for pt in hist["series"]["train|2027-01-09|301У"]["points"]] == [None, 2300, 2300]


def test_flight_appearing_gets_aviasales_link(project):
    root, fx, svc = project
    run(root, fx, "2026-11-03T09:00:00Z")
    write_fixture(fx, "flights-2027-01-10.json", tp_response([offer("101", 6190, departure_at="2027-01-10T10:20:00+03:00")]))
    run(root, fx, "2026-11-03T09:30:00Z")
    p = svc.messages[-1]["payload"]
    assert p["kind"] == "flight_appeared" and "Бугульма → Москва" in p["title"] and "10.01" in p["title"]
    assert p["links"][0]["url"] == "https://www.aviasales.ru/search/UUA1001MOW1"
    assert "6 190 ₽" in p["body"] and "10:20" in p["body"]


def test_identical_rerun_changes_no_files(project):
    root, fx, svc = project
    run(root, fx, "2026-11-03T09:00:00Z")
    snap = {p: p.read_bytes() for p in list(root.rglob("*.json")) if "fx" not in p.parts and "push" not in p.parts}
    run(root, fx, "2026-11-03T09:10:00Z", force=False)   # рейсы раньше чем через полчаса не проверяются
    run(root, fx, "2026-11-03T09:14:00Z", force=False)
    after = {p: p.read_bytes() for p in snap}
    assert after == snap          # ни одного изменённого байта → workflow не создаст коммит
    assert len(svc.messages) == 1


def test_dead_subscription_is_deleted(project):
    root, fx, svc = project
    svc.status = 410
    run(root, fx, "2026-11-03T09:00:00Z")
    assert list((root / "push").glob("*.json")) == []


def test_broken_config_reports_problem_once(project):
    root, fx, svc = project
    run(root, fx, "2026-11-03T09:00:00Z")
    (root / "config.yaml").write_text("train:\n  dates: [\"вчера\"]\n", encoding="utf-8")
    assert run(root, fx, "2026-11-03T10:00:00Z") == 1
    assert run(root, fx, "2026-11-03T10:10:00Z") == 1
    problems = [m for m in svc.messages if m["payload"]["kind"] == "problem"]
    assert len(problems) == 1 and "неверная дата" in problems[0]["payload"]["body"]


def test_digest_message_after_digest_hour(project):
    root, fx, svc = project
    run(root, fx, "2026-11-03T03:00:00Z")      # 06:00 МСК — рано; приветствие
    assert [m["payload"]["kind"] for m in svc.messages] == ["welcome"]
    run(root, fx, "2026-11-03T06:30:00Z")      # 09:30 МСК
    assert [m["payload"]["kind"] for m in svc.messages] == ["welcome", "digest"]
    run(root, fx, "2026-11-03T08:00:00Z")
    assert len(svc.messages) == 2
    run(root, fx, "2026-11-04T06:30:00Z")      # на следующий день — снова
    assert len(svc.messages) == 3


def test_quiet_hours_make_nonurgent_silent_but_never_urgent(project):
    root, fx, svc = project
    write_fixture(fx, "rzd-2027-01-09.json", rzd_response([train("301У", [group("Compartment", 3, 4200)])]))
    run(root, fx, "2026-11-03T00:00:00Z")      # 03:00 МСК: первое наблюдение уже с билетами — приветствие
    write_fixture(fx, "rzd-2027-01-09.json", rzd_response([train("301У", [group("Compartment", 3, 4800)])]))
    run(root, fx, "2026-11-03T00:10:00Z")
    assert svc.messages[-1]["payload"]["kind"] == "price_changed" and svc.messages[-1]["payload"]["silent"] is True
    write_fixture(fx, "rzd-2027-01-09.json", rzd_response([train("301У", []), train("303Я", [group("Compartment", 2, 5000)])]))
    run(root, fx, "2026-11-03T00:20:00Z")
    kinds = {m["payload"]["kind"]: m["payload"] for m in svc.messages}
    assert kinds["tickets_appeared"]["silent"] is False and kinds["sold_out"]["silent"] is True
