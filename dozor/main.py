"""Один запуск «Дозора»: проверить источники, уведомить, обновить данные для приложения."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from dozor.config import Config, ConfigError, load_config
from dozor.monitor import Monitor
from dozor.notify import Notifier
from dozor.push import Push
from dozor.sources import rzd, travelpayouts
from dozor.store import Store
from dozor.telegram import Telegram
from dozor.util import iso, msk, now_ts, parse_iso, today_msk

log = logging.getLogger("dozor")

PROBLEM_REPEAT = 12 * 3600
SUMMARY_BEAT = 15 * 60  # время проверки в сводке обновляется не чаще, чем раз в 15 минут (иначе коммит на каждый запуск)
SECRET_ENV = ("TRAVELPAYOUTS_TOKEN", "PUSH_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "RZD_PROXY", "RZD_BASE_URL", "TP_BASE_URL")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m dozor", description="Дозор билетов")
    parser.add_argument("--root", default=".", help="папка проекта (где config.yaml)")
    parser.add_argument("--fixtures", help="брать ответы источников из папки (rzd-ДАТА.json, flights-ДАТА.json), а не из интернета")
    parser.add_argument("--dry-run", action="store_true", help="не отправлять уведомления и не писать файлы, а напечатать")
    parser.add_argument("--now", help="время запуска в ISO, для тестов")
    parser.add_argument("--force", action="store_true", help="проверить рейсы, даже если с прошлой проверки прошло мало времени")
    parser.add_argument("--force-digest", action="store_true", help="отправить сводку сейчас")
    return parser.parse_args(argv)


def site_url_from_env(env: dict) -> str | None:
    if env.get("SITE_URL"):
        return env["SITE_URL"]
    repo = env.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


def fixture_fetchers(directory: Path) -> dict:
    """Источники, читающие сохранённые ответы: удобно проверять разбор и сценарии без интернета."""

    def rzd_fetch(cfg, date, ctx):
        path = directory / f"rzd-{date}.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"Trains": []}
        parsed = rzd.parse_train_pricing(payload)
        route = {"from": {"code": cfg.train.from_code or "2060500", "name": cfg.train.from_name}, "to": {"code": cfg.train.to_code or rzd.MOSCOW_CITY_CODE, "name": cfg.train.to_name}}
        return {"trains": parsed["trains"], "partial": parsed["partial"], "route": route, "meta": {"status": 200, "ms": 0}, "excerpt": ""}

    def tp_fetch(cfg, date, ctx):
        path = directory / f"flights-{date}.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"success": True, "data": []}
        return {"offers": travelpayouts.parse_flights(payload, cfg.flights.direct_only), "meta": {"status": 200, "ms": 0}, "excerpt": ""}

    return {("train", "rzd"): rzd_fetch, ("flight", "travelpayouts"): tp_fetch}


def report_problem(notifier: Notifier, state: dict, now: int, text: str) -> None:
    last = state.get("problem_at")
    if last and now - last < PROBLEM_REPEAT:
        return
    ev = {"kind": "problem", "urgent": False, "watch": "train", "tag": "problem", "title": "⚠️ Дозор: нужно ваше внимание", "body": text}
    result = notifier.send(ev, [], now)
    if result["push"]["ok"] or result["telegram"]:
        state["problem_at"] = now


def build_summary(mon: Monitor, state: dict, push: Push, now: int, known: list[str]) -> dict:
    return {
        "version": 1,
        "checked_at": iso(now),
        "repo": os.environ.get("GITHUB_REPOSITORY"),
        "push": {"vapid": push.public_key, "devices": known} if push.enabled else None,
        "watches": mon.summaries(),
        "sources": mon.sources_summary(),
        "events": [{k: e.get(k) for k in ("t", "kind", "watch", "urgent", "title", "body")} for e in state["events"][:40]],
    }


def stable_checked_at(store: Store, summary: dict, now: int) -> dict:
    """Не обновляем время проверки, пока остальное не изменилось и прошло меньше 15 минут: так не создаётся пустой коммит."""
    prev = store.load_summary()
    if prev.get("checked_at"):
        a = {k: v for k, v in prev.items() if k != "checked_at"}
        b = {k: v for k, v in summary.items() if k != "checked_at"}
        try:
            if a == b and now - parse_iso(prev["checked_at"]) < SUMMARY_BEAT:
                summary["checked_at"] = prev["checked_at"]
        except ValueError:
            pass
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(args.root).resolve()
    env = {k: v.strip() for k in SECRET_ENV if (v := os.environ.get(k, "")).strip()}
    env.update({k: os.environ[k] for k in ("GITHUB_REPOSITORY", "SITE_URL") if os.environ.get(k)})
    store = Store(root)
    state = store.load_state()
    history = store.load_history()
    now = parse_iso(args.now) if args.now else now_ts()
    site_url = site_url_from_env(env)

    push = Push(env.get("PUSH_SECRET") or env.get("TRAVELPAYOUTS_TOKEN"), root, site_url, dry_run=args.dry_run)
    tg = Telegram(env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"), dry_run=args.dry_run)

    try:
        cfg: Config = load_config(root / "config.yaml")
    except ConfigError as exc:
        log.error("Ошибка в config.yaml: %s", exc)
        from dozor.config import Notify

        notifier = Notifier(push, tg, state, Notify(), site_url)
        report_problem(notifier, state, now, f"Ошибка в config.yaml: {exc}")
        if not args.dry_run:
            store.save_state(state)
        return 1

    notifier = Notifier(push, tg, state, cfg.notify, site_url)
    fetchers = fixture_fetchers(Path(args.fixtures)) if args.fixtures else None
    mon = Monitor(cfg, state, history, env, now, fetchers=fetchers)
    events = mon.run(force=args.force)

    # --- уведомления -----------------------------------------------------------------------
    try:
        greeted = notifier.greet_new_devices(mon.digest_body() or "Слежу за билетами. Первая проверка идёт.", now)
        hour = cfg.notify.digest_hour
        if greeted and hour is not None and msk(now).hour >= hour:
            state["digest_day"] = today_msk(now)   # приветствие уже содержит сводку — вторую за день не шлём
        for ev in events:
            ev_links = mon.links(ev["watch"]) if ev.get("buy") else []
            delivery = notifier.send(ev, ev_links, now)
            state["events"].insert(0, {"t": now, **{k: ev.get(k) for k in ("kind", "watch", "urgent", "title", "body")}, "delivery": delivery})
        targets = bool(state.get("push", {}).get("known")) or notifier.telegram_ready()
        digest = mon.maybe_digest(targets)
        if args.force_digest and not digest:
            digest = {"kind": "digest", "urgent": False, "watch": mon.defs()[0].key if mon.defs() else "train", "tag": "digest", "title": "📋 Сводка: слежу за билетами", "body": mon.digest_body()}
        if digest:
            delivery = notifier.send(digest, [], now)
            state["events"].insert(0, {"t": now, **{k: digest.get(k) for k in ("kind", "watch", "urgent", "title", "body")}, "delivery": delivery})
        notifier.prune_known()
    except Exception:  # уведомления не должны мешать сохранить данные
        log.exception("Ошибка при отправке уведомлений")

    summary = stable_checked_at(store, build_summary(mon, state, push, now, state.get("push", {}).get("known", [])), now)

    if args.dry_run:
        for item in tg.outbox:
            print(f"\n----- telegram (silent={item['silent']}) -----\n{item['text']}")
        for item in push.outbox:
            print(f"\n----- push → {item['device']} (silent={item.get('silent')}, urgent={item.get('urgent')}) -----\n{item['title']}\n{item['body']}\n{item['url']}  {item['links']}")
        log.info("Проверка завершена (dry-run, файлы не записаны). Событий: %d", len(events))
        return 0

    store.save_history(history)
    store.save_summary(summary)
    store.save_state(state)
    log.info("Проверка завершена. Событий: %d", len(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
