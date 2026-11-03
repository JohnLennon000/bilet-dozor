import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "sw_harness.js"


@pytest.fixture(scope="module")
def sw():
    if not shutil.which("node"):
        pytest.skip("нет node: проверка service worker пропущена")
    return json.loads(subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=True, timeout=30).stdout)


def test_default_click_opens_app_card_when_no_window(sw):
    assert sw["defaultNoWindow"]["opened"] == ["https://me.github.io/bilet-dozor/#w=train:2027-01-09"]
    assert sw["defaultNoWindow"]["closed"] == 1


def test_default_click_reuses_open_app_window(sw):
    assert sw["defaultWithWindow"]["opened"] == []
    assert sw["navigatedTo"] == "https://me.github.io/bilet-dozor/#w=train:2027-01-09"


def test_buy_action_opens_the_service_link(sw):
    assert sw["action0"]["opened"] == ["https://ticket.rzd.ru/x"]


def test_unsafe_link_falls_back_to_app(sw):
    assert sw["unsafeAction"]["opened"] == ["https://me.github.io/bilet-dozor/#w=train:2027-01-09"]


def test_missing_data_opens_app_root(sw):
    assert sw["missingData"]["opened"] == ["https://me.github.io/bilet-dozor/"]


def test_garbage_push_still_shows_a_notification(sw):
    [n] = sw["garbagePush"]
    assert n["title"] == "Дозор билетов" and n["options"]["body"] == "просто текст"
