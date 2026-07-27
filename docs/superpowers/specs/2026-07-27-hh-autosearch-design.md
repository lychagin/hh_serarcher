# Дизайн: автопоиск вакансий на hh.ru

**Дата:** 2026-07-27
**Статус:** утверждён, готов к планированию реализации

## 1. Задача

Сервис для личного использования, который регулярно ищет вакансии на hh.ru по заданному
набору запросов, отсеивает нерелевантные, оценивает оставшиеся по профилю и присылает
только новые находки.

Развёртывание — Docker-контейнер на VPS. Первая версия выгружает результаты в CSV и
Markdown; следующая добавит личный Telegram-канал.

Исходные материалы: `hh_autosearch_plan.md` (карта из 30 запросов, схема весов),
`hh_api_python_plan.md` (набросок модулей).

## 2. Ключевые решения

| Решение | Выбор | Обоснование |
|---|---|---|
| Модель работы | Инкрементальный трекер | Состояние в SQLite, на выходе только новое с прошлого прогона. Без этого Telegram-канал утонет в дублях |
| Источник данных | RSS + JSON-LD | Публичный API закрыт (см. §3). RSS работает анонимно |
| Глубина анализа | Двухфазная | Дешёвый отсев по заголовку, затем полное описание только для выживших |
| Оценка релевантности | Ключевые слова | LLM — заложенная точка расширения, не реализуется сейчас |
| Хранилище | Файл SQLite на volume | Один писатель, десятки тысяч строк. Postgres — оверкилл |
| Запуск | Долгоживущий контейнер | Планировщик внутри, `restart: unless-stopped`. Вся конфигурация в git |
| Форматы отчёта | CSV + Markdown | Через общий интерфейс `Sink`, к которому позже подключается Telegram |
| Telegram | Личный канал | Персональные уведомления, не переопубликация |

## 3. Исследование источника данных

Проверено на живом hh.ru 2026-07-27.

### 3.1 Публичный API закрыт

```
GET https://api.hh.ru/vacancies?text=Yocto  → 403 {"errors":[{"type":"forbidden"}]}
GET https://api.hh.ru/areas/113             → 200 OK
```

403 воспроизводится при любом `User-Agent`. Поддержка API для соискателей прекращена
15.12.2025 (предупреждение на форме регистрации приложения на dev.hh.ru), токен получить
нельзя. Справочники (`/areas`, словари) остаются открытыми — источник кодов регионов.

### 3.2 RSS-лента поиска работает анонимно

```
GET https://hh.ru/search/vacancy/rss?text=Python&area=66&order_by=publication_time
→ 200 application/rss+xml
```

Без токена и кук. Каждый `<item>` содержит: `<link>` с id вакансии, `<title>`, `<pubDate>`
и `<description>` с компанией, регионом и уровнем дохода.

Поведение параметров проверено сравнением множеств id, а не кодов ответа:

| Параметр | Статус | Как проверено |
|---|---|---|
| `area=66` | работает | все 20 результатов — Нижний Новгород, 0 пересечений с нефильтрованной выдачей |
| `experience=between3And6` | работает | выдача меняется, 8 общих id из 20 |
| `schedule`, `employment` | принимаются | — |
| `period=1` | работает | из 20 вакансий осталась 1 |
| `order_by=publication_time` | работает | выдача пересортирована, 13 общих id из 20 с relevance |
| `page`, `items_on_page` | **игнорируются** | `page=1` вернул те же 20 id, что и `page=0` |

### 3.3 Ограничение: 20 вакансий на запрос, пагинации нет

Смягчается тремя факторами:

1. `order_by=publication_time` превращает окно в «20 самых свежих» — ровно то, что нужно
   инкрементальному трекеру.
2. Набор из ~50 узких запросов дробит выдачу; переполнить окно по «Yocto» или
   «Buildroot» практически невозможно.
3. Прогон раз в несколько часов делает окно ещё безопаснее.

Остаточный риск: пропуск возможен, если по одному запросу между прогонами появилось
более 20 вакансий. Начальный бэкфилл соберёт не всю историю, а 20 свежих на запрос;
дальше база наполняется сама.

### 3.4 Страница вакансии содержит JSON-LD

```
GET https://hh.ru/vacancy/135586311 → 200 (анонимно)
<script type="application/ld+json"> {"@type": "JobPosting", ...} </script>
```

Доступные поля: `title`, `description` (полный текст), `hiringOrganization`,
`jobLocation`, `datePosted`, `validThrough`, `identifier`. Структурированные данные, а не
вёрстка — устойчивее к редизайну. Поля `key_skills` в JSON-LD нет; ключевые технологии
извлекаются из текста описания.

### 3.5 Требование легитимности

Работаем только с публично отдаваемыми данными, без авторизации, кук и эмуляции браузера.
В `robots.txt` hh.ru секции `User-agent: *` нет; `Disallow: /rss/*` адресован только
Yandex и Googlebot и к пути `/search/vacancy/rss` не относится. Пользовательское
соглашение hh может отдельно ограничивать автоматизированный сбор — принятый режим работы
это учитывает.

Обязательные к реализации правила:

- честный `User-Agent` с названием проекта и контактным email;
- разбор `robots.txt` через `urllib.robotparser` и соблюдение его в рантайме;
- одно соединение за раз, пауза между запросами, никакой параллельности;
- соблюдение `429` и `Retry-After`, экспоненциальный backoff;
- при устойчивом `403` — остановка прогона, **никаких попыток обхода**;
- условные запросы (`If-Modified-Since` / `ETag`);
- вечный кэш описаний: одна вакансия скачивается ровно один раз;
- Telegram — личный канал, персональное потребление, не переопубликация.

Отвергнутые альтернативы: Playwright с залогиненной сессией и извлечение кук из Chrome.
Дают полную выдачу с пагинацией, но стоят браузера в образе (+700 МБ), хрупкости к
редизайну, поддержки живой сессии и куда более уверенного нарушения соглашения. Держим
в резерве на случай закрытия RSS.

## 4. Архитектура

### 4.1 Конвейер

```
1. DISCOVERY   ~50 запросов из config → RSS → DiscoveredVacancy      (~1000 записей)
2. DEDUP       отсеять id, уже известные базе                        (обычно единицы-десятки)
3. PREFILTER   отсев по заголовку и региону → status=rejected
4. ENRICH      GET hh.ru/vacancy/{id} → JSON-LD → полное описание
5. SCORE       Scorer.score() → score + разбивка + кластер
6. PERSIST     Repository.save() → SQLite, status=new
7. EMIT        Sink.emit() → CSV + Markdown (позже + Telegram)
```

Дешёвые шаги 2–3 стоят до дорогого шага 4: из ~1000 обнаруженных записей до скачивания
страниц доходят единицы. Низкая нагрузка на hh — следствие устройства конвейера, а не
искусственного торможения.

### 4.2 Точки расширения

| Интерфейс | Реализовано сейчас | Планируется |
|---|---|---|
| `Scorer` | `KeywordScorer` | `LlmScorer` — Claude, OpenAI или локальная модель |
| `Sink` | `CsvSink`, `MarkdownSink` | `TelegramSink` |
| `Repository` | `SqliteRepository` | `PostgresRepository` |

Каждый — протокол на два-три метода. Ни одно из будущих расширений не требует правок
в `pipeline.py`.

Провайдер LLM намеренно не фиксируется: интерфейс `Scorer` принимает вакансию и
возвращает оценку с разбивкой, всё остальное — деталь реализации.

### 4.3 Структура проекта

```text
hh_search/
  __main__.py           # CLI
  pipeline.py           # оркестрация семи шагов
  scheduler.py          # цикл режима serve
  config/
    models.py           # pydantic-схема конфига
    loader.py           # чтение YAML + валидация
  domain/
    models.py           # DiscoveredVacancy, VacancyDetails, ScoredVacancy
  sources/
    http.py             # вежливый клиент: UA, robots, троттлинг, backoff
    rss.py              # шаг 1
    vacancy_page.py     # шаг 4, извлечение JSON-LD
  filtering/
    matching.py         # нормализация и сопоставление слов
    prefilter.py        # шаг 3
  scoring/
    base.py             # протокол Scorer
    keyword.py          # шаг 5
  storage/
    repository.py       # весь SQL живёт здесь
    schema.sql
  sinks/
    base.py             # протокол Sink
    csv_sink.py
    markdown_sink.py
```

Границы: `sources/http.py` — единственный модуль, знающий про сеть;
`storage/repository.py` — единственный, знающий про SQL; `pipeline.py` не знает ни про то,
ни про другое и оперирует протоколами. Ориентир — не более ~150 строк на файл.

## 5. Модель данных

### 5.1 Схема SQLite

```sql
CREATE TABLE vacancy (
    id              TEXT PRIMARY KEY,     -- id с hh, ключ дедупликации
    url             TEXT NOT NULL,
    -- из RSS (шаг 1)
    title           TEXT NOT NULL,
    company         TEXT,
    area            TEXT,
    salary_raw      TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    salary_currency TEXT,
    published_at    TEXT NOT NULL,        -- ISO-8601
    -- из JSON-LD (шаг 4)
    description     TEXT,                 -- NULL = ещё не обогащена
    fetched_at      TEXT,
    enrich_attempts INTEGER NOT NULL DEFAULT 0,
    -- из скоринга (шаг 5)
    score           REAL,
    score_detail    TEXT,                 -- JSON с разбивкой
    cluster         TEXT,                 -- backend | embedded | ai | telecom
    -- жизненный цикл
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    first_seen_at   TEXT NOT NULL,
    reported_at     TEXT
);

CREATE TABLE vacancy_query (
    vacancy_id TEXT NOT NULL REFERENCES vacancy(id),
    query      TEXT NOT NULL,
    PRIMARY KEY (vacancy_id, query)
);

CREATE TABLE run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,            -- ok | partial | failed
    discovered  INTEGER DEFAULT 0,
    new_count   INTEGER DEFAULT 0,
    rejected    INTEGER DEFAULT 0,
    enriched    INTEGER DEFAULT 0,
    reported    INTEGER DEFAULT 0,
    error       TEXT
);
```

`vacancy_query` — основа для кластера и инструмент отладки конфига: показывает, какой
запрос сколько вакансий приносит и какая доля отсеивается. `run` даёт статистику по
времени и питает healthcheck.

### 5.2 Жизненный цикл

```
rejected  ← prefilter отсеял (никогда не скачивается)
new       ← обогащена и оценена
reported  ← sink отработал успешно
interesting / applied / archived  ← вручную
```

Сохранение идёт **до** отправки, а отправка выбирает из базы по `status='new'`, а не из
списка в памяти. Падение между шагами 6 и 7 не теряет данные — следующий прогон доотправит.

`enrich_attempts` защищает от вечных повторов: после 3 неудачных попыток скачать
(удалена, 404) вакансия помечается `rejected` с причиной `enrich_failed`.

### 5.3 Доменные модели

- `DiscoveredVacancy` — результат шага 1: id, url, title, company, area, salary_raw,
  published_at, found_by_query.
- `VacancyDetails` — результат шага 4: description, valid_through, location.
- `ScoreBreakdown` — title, stack, responsibilities, domain, penalty, total, matched.
- `ScoredVacancy` — объединение выше плюс cluster.

Все — pydantic-модели.

## 6. Скоринг

```
score = 100 × (0.40·title + 0.30·stack + 0.20·responsibilities + 0.10·domain) − penalty
```

**title (40%)** — по заголовку, грубая шкала: 0 — ничего, 0.5 — есть роль или технология,
1.0 — есть и то, и другое.

**stack (30%)** и **responsibilities (20%)** — по полному описанию, с насыщением:
`min(найдено / целевое, 1.0)`, целевое ≈ 5 и ≈ 3 соответственно. Насыщение обязательно:
без него скоринг измеряет длину описания, а не релевантность.

**domain (10%)** — отрасль компании и контекст.

**penalty** — вычитается, а не умножается: каждый негативный сигнал стоит ~15 очков. Одно
случайное слово не убивает хорошую вакансию, три — убивают.

Разбивка сохраняется в `score_detail`, включая списки конкретных совпавших слов:

```json
{"title": 1.0, "stack": 0.8, "responsibilities": 0.67, "domain": 1.0,
 "penalty": 0, "total": 87.4,
 "matched": {"stack": ["Yocto", "C++", "Buildroot", "ARM"],
             "responsibilities": ["архитектур", "код-ревью"]}}
```

Без ответа на вопрос «почему 87?» настроить веса невозможно.

### 6.1 Сопоставление слов (`filtering/matching.py`)

Описания на русском, наивный `keyword in text` даёт мусор. Обязательно учесть:

- **Морфология.** «разработка / разработке / разработчика» — матч по основе (`разработ`),
  а не по точной форме. `pymorphy` не берём: для полусотни слов префикс основы достаточен.
- **Границы слов.** `lead` не должен ловиться в `leadership`, `go` — в `google`.
  Матчинг по `\b`, а не подстрокой.
- **Спецсимволы.** `C++`, `C#`, `.NET` ломают наивные регулярки — экранирование
  и отдельная обработка границ.
- **Кириллица-латиница.** `1С` пишут и кириллической `С`, и латинской `C`; то же
  с `С++`. Без нормализации похожих символов стоп-слово молча не срабатывает.
- **Регистр и `ё`.** Нижний регистр, `ё` → `е`.

Списки слов компилируются в регулярки один раз при старте. Тесты на все перечисленные
случаи пишутся сразу.

### 6.2 Кластер

Берётся из конфига по запросу, которым вакансия найдена. При нахождении несколькими
запросами из разных кластеров побеждает запрос с наибольшим `weight`.

### 6.3 Что попадает в отчёт

Жёсткого порога нет. В CSV идёт всё новое. В Markdown — всё новое, разделённое на «Топ»
(`score >= report_threshold`, по умолчанию 60) с выжимкой и «Остальное» одной строкой на
вакансию. Причина: при десятке новых вакансий в день порог экономит секунды чтения, но
рискует спрятать хорошую вакансию из-за неотлаженного списка ключевых слов. Раздел
«Остальное» служит обратной связью по качеству скоринга.

## 7. Конфигурация

Три файла, разделённые по частоте изменений:

```
/data/config/
  queries.yaml    # часто — набор запросов
  profile.yaml    # регулярно — сигналы, веса, стоп-слова
  app.yaml        # редко — расписание, sink'и, троттлинг
```

```yaml
# queries.yaml
defaults:
  experience: [between3And6, moreThan6]
  employment: full
queries:
  - text: "Backend Team Lead"
    cluster: backend
    weight: 10
    area: [66]
  - text: "Backend Team Lead"
    cluster: backend
    weight: 10
    schedule: remote
  - text: "Yocto"
    cluster: embedded
    weight: 9
```

RSS не умеет «регион ИЛИ удалёнка», поэтому такие комбинации разворачиваются в отдельные
запросы; дедупликация по `id` склеивает пересечения. 30 строк исходного плана дают
примерно 50 запросов — меньше минуты вежливого обхода.

```yaml
# profile.yaml
weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}
saturation: {stack: 5, responsibilities: 3}
penalty_per_signal: 15
signals:
  title_roles:  [team lead, tech lead, teamlead, ведущий, senior, старший]
  title_tech:   [backend, embedded, linux, c++, python, node]
  stack:        [yocto, buildroot, openwrt, bsp, kubernetes, kafka, postgresql,
                 clickhouse, docker, llm, rag, mcp]
  responsibilities: [архитектур, менторинг, код-ревью, проектирован, техдолг]
  domain:       [телеком, встраиваем, embedded, iot]
negative:       [junior, стажёр, intern, 1c, продаж, рекрутер, ручное тестирование]
report_threshold: 60
```

```yaml
# app.yaml
contact_email: "serg.lychagin.usa@gmail.com"   # попадает в User-Agent
user_agent: "hh-search/0.1 (personal job search; {contact_email})"
schedule:
  interval_hours: 4
http:
  delay_between_requests_sec: 1.0
  timeout_sec: 20
  max_retries: 3
  respect_robots: true
enrich:
  max_attempts: 3
sinks: [csv, markdown]        # позже добавится telegram
paths:
  state: /data/state/hh.db
  reports: /data/reports
  logs: /data/logs
```

Конфиг валидируется pydantic при старте: опечатка роняет процесс сразу с внятным
сообщением, до первого сетевого запроса.

Секретов в YAML нет. Токен бота и будущий ключ LLM — только переменные окружения
(`HH_TELEGRAM_TOKEN`, `HH_TELEGRAM_CHAT_ID`). В репозитории — `.env.example`;
`.env` и `data/` — в `.gitignore`.

## 8. Развёртывание

### 8.1 Раскладка volume

```
/data/
  config/     # queries.yaml, profile.yaml, app.yaml — правятся на хосте
  state/      # hh.db
  reports/    # 2026-07-27-new.csv, 2026-07-27-new.md
  logs/       # hh.log
```

### 8.2 Docker

```yaml
services:
  hh-search:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data
    environment:
      TZ: Europe/Moscow
    healthcheck:
      test: ["CMD", "python", "-m", "hh_search", "healthcheck"]
      interval: 15m
      retries: 3
```

Образ — `python:3.12-slim`, запуск от непривилегированного пользователя, без браузеров,
ориентир по размеру ~150 МБ. Точка входа — `serve`: планировщик спит и раз в
`schedule.interval_hours` (по умолчанию 4) вызывает ту же функцию, что и `run --once`.

`healthcheck` смотрит в таблицу `run` и падает, если последний **успешный** прогон старше
двух интервалов. Ловит сценарий «процесс жив, работа не делается».

### 8.3 CLI

```
python -m hh_search run --once        # разовый прогон
python -m hh_search serve             # демон (точка входа Docker)
python -m hh_search init-db           # создать схему
python -m hh_search healthcheck       # для Docker HEALTHCHECK
python -m hh_search report --since 7d # перегенерировать отчёт из базы
python -m hh_search mark <id> applied # пометить статус вручную
```

## 9. Обработка ошибок

| Ситуация | Реакция |
|---|---|
| Таймаут/сеть на одном запросе | `WARNING`, остальные запросы продолжаются, прогон `partial` |
| `429` + `Retry-After` | Ждём указанное время, экспоненциальный backoff, до 3 попыток |
| Устойчивый `403` | Остановка прогона, `status=failed`, громкий лог. Обходных путей нет |
| На странице нет JSON-LD | `enrich_attempts++`, вакансия ждёт следующего прогона |
| >50% обогащений провалились за прогон | Громкая тревога: вероятно, hh сменил вёрстку |
| Sink упал | Вакансия остаётся `new`, доотправится следующим прогоном |
| Конфиг невалиден | Падение на старте, до сетевых запросов |

Правило про 50% — канарейка на смену формата источника: проблема обнаруживается в тот же
день, а не через месяц по пустым отчётам.

Логи — в stdout (подхватывает `docker logs`) и в `/data/logs/hh.log` с ротацией.

## 10. Тестирование

**Юнит-тесты:**
- нормализация и матчинг: кириллическая `С` в `1С`, `C++`, `lead` внутри `leadership`,
  морфологические формы;
- арифметика скоринга, включая насыщение и штрафы;
- разбор зарплаты из строки RSS.

**Тесты парсинга — на зафиксированных фикстурах.** Живые ответы (RSS-лента и HTML
страницы вакансии) сохраняются в `tests/fixtures/` при реализации; парсеры тестируются
без сети.

**Интеграционный тест конвейера** — фейковый источник, SQLite в памяти, фейковые sink'и.
Проверяет дедупликацию между прогонами, что отклонённые не скачиваются повторно, и что
падение sink'а оставляет вакансию в `new`.

**CI (GitHub Actions):** `ruff` → `mypy` → `pytest` с замоканным HTTP (`respx`). Сеть
в CI не используется — живая выдача hh сделала бы сборку флаки.

**Контрактный тест** с меткой `@pytest.mark.network`: ходит в живой hh.ru и проверяет, что
RSS отдаёт `<item>` со ссылками на вакансии, а страница вакансии содержит `JobPosting`.
В CI пропускается, запускается вручную (`pytest -m network`). Ранняя система оповещения
об изменении источника.

## 11. Зависимости

Рантайм: `httpx`, `pydantic`, `pydantic-settings`, `PyYAML`, `typer`.
Разработка: `pytest`, `respx`, `ruff`, `mypy`. Сборка — `uv`.

Сознательно не используются:

- `feedparser` — RSS у hh простой и корректный, достаточно `xml.etree` из стандартной
  библиотеки;
- HTML-парсер — блок `<script type="application/ld+json">` извлекается регуляркой
  (проверено на живой странице);
- `pandas` — для CSV из десятков строк достаточно модуля `csv`.

## 12. Границы первой версии

**Входит:** RSS-дискавери, дедупликация, префильтр, обогащение через JSON-LD,
keyword-скоринг, SQLite, CSV- и Markdown-отчёты, Docker с планировщиком, CLI, тесты, CI.

**Не входит (точки расширения подготовлены):** Telegram-sink, LLM-оценка, генерация
сопроводительных писем, интерактивная смена статуса кнопками, Postgres, Playwright.

## 13. Предусловия и открытые вопросы

- Регистрация приложения на dev.hh.ru **не требуется** — выбранный подход не использует
  авторизацию.
- Контактный email для `User-Agent` задаётся в `app.yaml` при первом развёртывании.
- Коды регионов уточняются через открытый `https://api.hh.ru/areas`; `66` = Нижний
  Новгород использован при проверке и подтверждён содержимым выдачи.
- Точные допустимые значения `experience`, `schedule`, `employment` для RSS фиксируются
  при реализации по справочникам `api.hh.ru/dictionaries` и проверяются контрактным
  тестом.
