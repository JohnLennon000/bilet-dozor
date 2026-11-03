from dozor.links import flight_links, train_links
from dozor.util import slug
from tests.helpers import make_cfg


def test_slug():
    assert slug("Туймазы") == "tuymazy" and slug("Москва") == "moskva" and slug("Нижний Новгород") == "nizhniy-novgorod"


def test_train_links_use_resolved_station_codes():
    cfg = make_cfg()
    route = {"from": {"code": "2060500"}, "to": {"code": "2000000"}}
    links = train_links(cfg, route, "2027-01-09")
    assert links[0] == {"label": "Купить на РЖД", "url": "https://ticket.rzd.ru/searchresults/v/1/2060500/2000000/2027-01-09", "primary": True}
    assert links[1]["url"] == "https://travel.yandex.ru/trains/tuymazy--moskva/?when=2027-01-09"
    assert links[2]["url"] == "https://www.tutu.ru/poezda/Tuymazy/Moskva/"


def test_missing_codes_fall_back_to_rzd_home_and_drop_unfillable_links():
    cfg = make_cfg(train={"from_code": "", "to_code": ""})
    links = train_links(cfg, None, "2027-01-09")
    assert links[0]["url"] == "https://ticket.rzd.ru/" and links[0]["primary"]
    assert len(links) == 3  # у Яндекса и Туту коды станций не нужны


def test_flight_links():
    assert flight_links(make_cfg(), "2027-01-09")[0]["url"] == "https://www.aviasales.ru/search/UUA0901MOW1"
