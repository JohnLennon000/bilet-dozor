# Дозор билетов — передача в новый чат (10 ноября 2026)

Это вводная для нового чата Claude по репозиторию `JohnLennon000/bilet-dozor`. Прочитай её целиком, затем `CLAUDE.md` (подробная история и правила кода) и скилл `.claude/skills/dozor-biletov/` (как сопровождать и разбирать неполадки).

## Кто пользователь и как с ним работать

- Говорит по-русски — отвечать по-русски, **коротко, по шагам**.
- Чаще всего пишет **с Android-телефона**, иногда сидит за ПК с Windows. Шаги давать такие, чтобы их можно было сделать с телефона.
- Перед любыми действиями на его устройствах сначала говорить, что собираешься делать. Токены и ключи в чат не просить.
- Разрешил коммитить и пушить прямо в `main`.
- Идея следить за соседними станциями посадки (Уфа, Белебей) отклонена — не предлагать.

## Что делает сервис

- **Поезд:** Туймазы → Москва, 09.01.2027, поезд **301У** — мест пока нет; ждём, когда появятся (добавят вагоны или дополнительный поезд).
- **Самолёт:** Бугульма (UUA) → Москва, прямой, **09 или 10.01.2027** — в кэше Aviasales рейсов пока нет.
- При появлении билетов — push-уведомление на телефон и динамика цен в приложении.
- Приложение: https://johnlennon000.github.io/bilet-dozor/ (PWA установлено на Android, push подключён: `push/e1eeff9faec7.json`).

## Как устроено сейчас (всё работает)

```
cron-job.org (каждые 10 мин) → GitHub Actions «Проверка билетов» (check.yml, workflow_dispatch с inputs.cron=true)
  → Tailscale → телефон Redmi Note 7 в России (exit node) → ticket.rzd.ru
  → api.travelpayouts.com (рейсы, раз в 30 мин)
  → Web Push → коммит docs/data/*.json, state/state.json → PWA на GitHub Pages
```

- **Телефон-посредник:** Redmi Note 7, с 7 ноября на прошивке **LunarisAOSP 3.12** (Android 16) вместо MIUI — MIUI каждую ночь в 02:39 убивала Tailscale. В tailnet `johnlennon000.github` устройство `redmi-note-7`, exit node разрешён. С 8 ноября отвалов телефона нет.
- **Короткие сбои РЖД около 03:30 МСК** (одна проверка, таймаут при живом телефоне) — похоже на техработы РЖД, не страшно.
- **Будильник:** cron-job.org, задание «Дозор билетов». Расписание GitHub в `check.yml` — запасное (срабатывает редко).
- **Сертификат РЖД** выдан УЦ Минцифры — в `check.yml` есть шаг, добавляющий `certs/russian_trusted_root_ca.pem` к certifi. `verify=False` не использовать.
- **Секреты GitHub:** `TRAVELPAYOUTS_TOKEN`, `TS_AUTHKEY` (истекает около **2 февраля 2027**). Токен GitHub для cron-job.org — срок выбирал пользователь; напомнить продлить до 10 января.

Состояние за секунду: `git fetch -q origin main && python3 .claude/skills/dozor-biletov/scripts/status.py`.
Журнал запусков — GitHub MCP: `actions_list` (`check.yml`) → `list_workflow_jobs` → `get_job_logs`. Главная строка: `устройство: Redmi Note 7 | в сети: да | выход разрешён: да` и `Страна выхода: RU`.

## Текущая задача — ЮВТ Аэро

Пользователь хочет получать цены авиакомпании **ЮВТ Аэро** (код RT, базируется в Бугульме, сайт https://uvtaero.ru/) напрямую, не дожидаясь кэша Aviasales.

Что уже выяснено:
- Продажа на сайте — через Websky (Сирена). Форма поиска шлёт GraphQL на `https://rt.webskyx.com/graphql/query/nemo`.
- В скрипте формы (`https://cdn.webskyx.com/RT/static/SearchForm.min.js`) есть запросы:
  - `GetAvailableDates` — `FlightsSchedule(parameters: { departure: $departure, arrival: $arrival }) { datesData { date } flightProbability }` — на какие даты есть рейсы;
  - `MinPrices` / `FlightsMinPricesInPeriod($params: FlightsMinPricesInPeriodParameters!)` → `datesWithLowestPrices { date info price { amount currency } }` — минимальные цены по датам. Поля `FlightsMinPricesInPeriodParameters` ещё не выяснены — смотреть в том же скрипте, где формируются переменные запроса.
- Это неофициальный API. В прошлом чате прямой запрос блокировала система безопасности. Пользователь **явно разрешил** его: создал `.claude/settings.json` с правилом
  `Bash(curl -sS -m 25 -X POST https://rt.webskyx.com/graphql/query/nemo:*)` — запрос делать **ровно этой командой** (curl -sS -m 25 -X POST <этот адрес> …), без маскировки под браузер.

Что сделать:
1. Запросить расписание:
   ```bash
   curl -sS -m 25 -X POST https://rt.webskyx.com/graphql/query/nemo -H 'content-type: application/json' -d '{"operationName":"GetAvailableDates","variables":{"departure":"UUA","arrival":"MOW"},"query":"query GetAvailableDates($departure: String!, $arrival: String!) { FlightsSchedule(parameters: { departure: $departure, arrival: $arrival }) { datesData { date } flightProbability } }"}'
   ```
   Если `MOW` не принимается — попробовать конкретные аэропорты (`VKO`, `DME`, `SVO`, `ZIA`).
2. Найти параметры `FlightsMinPricesInPeriod` и запросить цены на 9–10 января 2027.
3. Рассказать пользователю, есть ли рейсы и цены на эти даты.
4. Если ответ полезный — предложить подключить ЮВТ Аэро как второй источник рейсов (`dozor/sources/uvt.py` по образцу `travelpayouts.py`, регистрация в `dozor/sources/__init__.py` и `FLIGHT_SOURCES` в `dozor/config.py`, настоящий ответ — в `tests/fixtures/` с тестом). Запросы из GitHub Actions пойдут через телефон (российский адрес) — проверить, что API отвечает и оттуда. Предупредить: API может измениться без предупреждения.
5. Если запрос и в новом чате заблокируют — сказать пользователю честно, обходить запрет нельзя. Запасной вариант, который он одобрит сразу: кнопка «ЮВТ Аэро» (https://uvtaero.ru/) в карточке рейсов (`flights.links` в `config.yaml`).

## Правила (коротко; полностью — в CLAUDE.md)

- После правок кода: `pip install pytest` при необходимости, `python -m pytest -q` (≈91 тест), коммит с понятным русским сообщением, `git push origin HEAD:main`.
- Изменил `docs/app.js`/`style.css` — поднять `CACHE` в `docs/sw.js`.
- Не добавлять в данные поля, меняющиеся каждый запуск (лишние коммиты).
- Ручной `python -m dozor` на копии репозитория с папкой `push/` отправит настоящий push — в копии удалять `push/`.
- Туту как источник не подключать без отдельного решения пользователя (внутренний API).
- Итоги работы дописывать в `CLAUDE.md` (и в скилл, если меняется схема) — это память между чатами.
