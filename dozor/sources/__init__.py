"""Реестр источников. Чтобы подключить новый (например, Туту), достаточно:

1. создать модуль в этой папке с константами NAME, TITLE, KIND («train» или «flight»),
   функциями check_env(env) → текст ошибки или None и fetch(cfg, date, ctx) → словарь (см. rzd.py и travelpayouts.py);
2. добавить его в TRAIN или FLIGHT ниже и в TRAIN_SOURCES / FLIGHT_SOURCES в dozor/config.py;
3. положить настоящий ответ сайта в tests/fixtures и написать тест разбора.
Монитор сам объединит данные нескольких источников и отследит сбой каждого отдельно.
"""
from dozor.sources import rzd, travelpayouts

TRAIN = {rzd.NAME: rzd}
FLIGHT = {travelpayouts.NAME: travelpayouts}


def get(kind: str, name: str):
    return (TRAIN if kind == "train" else FLIGHT)[name]
