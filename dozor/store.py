"""Файлы данных: state/state.json (память сервиса) и docs/data/*.json (их читает приложение).

Все записи идут через write_if_changed: если содержимое не изменилось, файл не трогается, а значит
и коммит в репозитории не создаётся. Проверка каждые 10 минут не засоряет историю.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAX_EVENTS = 100
MAX_POINTS = 4000


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        log.warning("%s повреждён — начинаю заново", what)
        return {}


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state" / "state.json"
        self.data_dir = self.root / "docs" / "data"
        self.history_path = self.data_dir / "history.json"
        self.summary_path = self.data_dir / "summary.json"

    def load_state(self) -> dict:
        state = _read_json(self.state_path, "state.json")
        state.setdefault("watches", {})
        state.setdefault("sources", {})
        state.setdefault("events", [])
        return state

    def save_state(self, state: dict) -> bool:
        del state["events"][MAX_EVENTS:]
        return write_if_changed(self.state_path, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    def load_history(self) -> dict:
        history = _read_json(self.history_path, "history.json")
        history.setdefault("series", {})
        return history

    def save_history(self, history: dict) -> bool:
        for ser in history["series"].values():
            del ser["points"][: max(0, len(ser["points"]) - MAX_POINTS)]
        return write_if_changed(self.history_path, json.dumps(history, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")

    def save_summary(self, summary: dict) -> bool:
        return write_if_changed(self.summary_path, json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")

    def load_summary(self) -> dict:
        return _read_json(self.summary_path, "summary.json")
