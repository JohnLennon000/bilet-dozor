import pytest

from dozor.sources import rzd
from dozor.util import SourceError
from tests.helpers import group, rzd_response, train


def test_sold_out_train_has_zero_seats():
    r = rzd.parse_train_pricing(rzd_response([train("301У", [])]))
    t = r["trains"][0]
    assert (t["number"], t["seats"], t["min_price"], t["groups"], t["no_data"]) == ("301У", 0, None, [], False)


def test_groups_seats_and_prices():
    r = rzd.parse_train_pricing(rzd_response([train("301У", [group("Compartment", 4, 4200), group("ReservedSeat", 10, 2300), group("Soft", 0, 9000)])]))
    t = r["trains"][0]
    assert t["seats"] == 14 and t["min_price"] == 2300
    assert [(g["label"], g["seats"]) for g in t["groups"]] == [("Купе", 4), ("Плацкарт", 10)]  # группы без мест отбрасываются


def test_train_from_schedule_without_car_groups_is_marked_no_data():
    t = rzd.parse_train_pricing(rzd_response([train("999Я", None)]))["trains"][0]
    assert t["no_data"] and t["seats"] == 0


def test_seats_summed_from_berth_categories():
    g = {"CarType": "Compartment", "CarTypeName": "Купе", "MinPrice": 4000, "LowerPlaceQuantity": 2, "UpperPlaceQuantity": 3}
    assert rzd.parse_train_pricing(rzd_response([train("1А", [g])]))["trains"][0]["seats"] == 5


def test_unknown_seat_count_means_seats_exist_better_false_alarm():
    g = {"CarType": "Compartment", "CarTypeName": "Купе", "MinPrice": 4000}
    t = rzd.parse_train_pricing(rzd_response([train("1А", [g])]))["trains"][0]
    assert t["seats"] == 1 and t["groups"][0]["approx"]


def test_same_class_groups_merge():
    t = rzd.parse_train_pricing(rzd_response([train("1А", [group("Compartment", 2, 5000), group("Compartment", 3, 4500)])]))["trains"][0]
    assert t["seats"] == 5 and t["min_price"] == 4500 and len(t["groups"]) == 1


def test_data_wrapper_and_partial_flag():
    r = rzd.parse_train_pricing({"data": rzd_response([train()], NotAllTrainsReturned=True)})
    assert r["partial"] and len(r["trains"]) == 1


@pytest.mark.parametrize("payload,kind", [
    ({"foo": 1}, "schema"),
    ("html", "schema"),
    ({"errorInfo": {"Code": 5, "Message": "нет"}}, "api"),
    ({"message": "Сервис недоступен"}, "api"),
    ({"Trains": [{"x": 1}]}, "schema"),
    ({"Trains": [train("1А", None, CarGroups="oops")]}, "schema"),
])
def test_format_surprises_become_visible_errors_not_silence(payload, kind):
    with pytest.raises(SourceError) as e:
        rzd.parse_train_pricing(payload)
    assert e.value.kind == kind


def test_empty_list_is_valid_no_trains():
    assert rzd.parse_train_pricing({"Trains": []})["trains"] == []


def test_suggestions_and_station_choice():
    payload = {"city": [{"name": "Москва", "ExpressCode": "2000000", "nodeType": "city"}],
               "train": [{"name": "Туймазы", "ExpressCode": "2024750", "nodeType": "station"}, {"name": "Туймазы-2", "ExpressCode": "1", "nodeType": "station"}]}
    c = rzd.parse_suggestions(payload)
    assert rzd.pick_station(c, "туймазы", "station")["code"] == "2024750"
    assert rzd.pick_station(c, "Москва", "city")["code"] == "2000000"
    assert rzd.pick_station(c, "Урюпинск") is None


def test_suggestions_alternative_shape():
    c = rzd.parse_suggestions([{"Name": "Туймазы", "Codes": {"Railway": "2024750"}, "SubType": "Станция"}])
    assert c[0]["code"] == "2024750"
