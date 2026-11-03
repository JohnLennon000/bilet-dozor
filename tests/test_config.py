import datetime as dt

import pytest
import yaml

from dozor.config import ConfigError, load_config, parse_config


def test_repo_config_is_valid_and_matches_the_request():
    cfg = load_config(__import__("pathlib").Path(__file__).parent.parent / "config.yaml")
    assert cfg.train.from_name == "Туймазы" and cfg.train.to_name == "Москва"
    assert cfg.train.dates == ["2027-01-09"] and cfg.train.trains == []
    assert (cfg.flights.origin, cfg.flights.destination) == ("UUA", "MOW")
    assert cfg.flights.dates == ["2027-01-09", "2027-01-10"] and cfg.flights.direct_only
    assert cfg.train.links[0]["primary"] and "rzd" in cfg.train.links[0]["url"]
    assert [l["label"] for l in cfg.train.links] == ["Купить на РЖД", "Яндекс Путешествия", "Туту"]


def test_yaml_dates_without_quotes_are_accepted():
    cfg = parse_config(yaml.safe_load("train:\n  dates: [2027-01-09]\n"))
    assert cfg.train.dates == ["2027-01-09"] and isinstance(dt.date.fromisoformat(cfg.train.dates[0]), dt.date)


@pytest.mark.parametrize("raw,fragment", [
    ({"trian": {}}, "trian"),
    ({"train": {"dates": ["2027-13-40"]}}, "неверная дата"),
    ({"train": {"dates": []}}, "список"),
    ({"train": {"sources": ["tutu"]}}, "неизвестный источник"),
    ({"train": {"links": [{"label": "x", "url": "http://a.b"}]}}, "https://"),
    ({"train": {"links": [{"label": "x"}]}}, "label и url"),
    ({"train": {"from_code": "abc"}}, "только цифры"),
    ({"flights": {"origin": "бугульма"}}, "IATA"),
    ({"flights": {"interval_min": 1}}, "от 5"),
    ({"flights": {"currency": "gbp"}}, "rub"),
    ({"notify": {"price_min_pct": 500}}, "0 до 100"),
    ({"notify": {"digest_hour": 30}}, "от 0 до 23"),
    ({"notify": {"sold_out": "да"}}, "true или false"),
])
def test_errors_are_readable_russian(raw, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(raw)


def test_broken_yaml_gives_hint(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("train:\n  from: [Туймазы\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="не читается"):
        load_config(p)


def test_quiet_hours_wrap_midnight():
    n = parse_config({"notify": {"quiet_from": 23, "quiet_to": 8}}).notify
    assert n.is_quiet(23) and n.is_quiet(3) and not n.is_quiet(8) and not n.is_quiet(12)
    off = parse_config({"notify": {"quiet_from": None}}).notify
    assert not off.is_quiet(3)


def test_only_one_primary_link_survives():
    cfg = parse_config({"train": {"links": [{"label": "a", "url": "https://a.b", "primary": True}, {"label": "b", "url": "https://c.d", "primary": True}]}})
    assert [l["primary"] for l in cfg.train.links] == [True, False]
