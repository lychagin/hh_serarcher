# Регион и формат работы — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить второй поток discovery для удалённой работы по всей России и штрафовать в скоринге вакансии вне домашнего региона, не предлагающие удалёнку.

**Architecture:** `QuerySpec` получает типизированное поле `work_format`, от которого зависит форма URL листинга. Формат работы извлекается со страницы вакансии как перечисление, хранится новой колонкой, участвует в скоринге отдельным штрафом и показывается во всех трёх приёмниках. Обход региональных поддоменов не делается — измерения показали, что регион задаёт хост, а параметр `area` игнорируется.

**Tech Stack:** Python 3.12, pydantic, sqlite3, httpx, pytest. Новых зависимостей проект не получает.

**Спека:** `docs/superpowers/specs/2026-07-30-region-and-work-format-design.md`. При расхождении плана и спеки верна спека.

## Global Constraints

- Ворота, зелёные после КАЖДОГО коммита: `uv run pytest`, `uv run mypy --strict hh_search tests`, `uv run ruff check .`, `uv run ruff format --check .`
- `line-length = 100`, `target-version = py312`. Новых зависимостей нет.
- Тесты не ходят в сеть. Маркер `network` — только для контрактных тестов против живого hh.ru.
- **robots.txt соблюдается, блокировки не обходятся.** Единственный используемый параметр — `work_format`, и только вместе с `&page=`. Живая фикстура правил — `tests/fixtures/robots_hh.txt`.
- §4.3 основной спеки держит бюджет 150 строк кода на модуль и перечень всех модулей пакета — это сторожат тесты. **Модуль, перерастающий бюджет, разделяется, а не получает строку-исключение** (решение владельца, принятое трижды).
- Утверждение документа, которое дёшево сверить исполнением, сверяется тестом, а не обещанием.
- Ветка: `region-work-format`, ответвляется от `telegram-sink`.
- Мутационный харнесс врёт дважды: обязательны `NO_COLOR=1`, `PYTHONDONTWRITEBYTECODE=1` и чистка `__pycache__`.

---

## Файловая структура

| Файл | Что меняется |
|---|---|
| `hh_search/config/models.py` | `WorkFormat` (enum), `QuerySpec.work_format`, ключ уникальности `(slug, work_format)`, `LocationConfig` в `ProfileConfig` |
| `hh_search/sources/listing.py` | `build_listing_url` учитывает `work_format` |
| `hh_search/sources/vacancy_page.py` | извлечение `workFormats`, агрегатный сторож дрейфа |
| `hh_search/domain/models.py` | `VacancyDetails.work_formats` |
| `hh_search/storage/schema.sql`, `migrations.py`, `mappers.py` | колонка `vacancy.work_formats` |
| `hh_search/scoring/keyword.py` | штраф за неудалёнку вне домашнего региона |
| `hh_search/sinks/csv_sink.py`, `markdown_sink.py`, `html_report.py` | формат в отчётах |
| `config.example/queries.yaml`, `profile.yaml` | образцы двух потоков и раздела `location` |
| `README.md` | раздел про два потока и штраф |

Модуль `vacancy_page.py` уже 13.8 КБ и близок к бюджету. Если извлечение форматов его перерастит — выносить форматы в `hh_search/sources/work_format.py` рядом с `salary.py`, который вынесен по той же причине.

---

### Task 1: `work_format` в конфиге и форма URL

**Files:**
- Modify: `hh_search/config/models.py` (`QuerySpec`, `QueriesConfig.reject_duplicate_slugs`)
- Modify: `hh_search/sources/listing.py` (`build_listing_url`)
- Test: `tests/test_config.py`, `tests/test_listing.py`

**Interfaces:**
- Consumes: `Slug`, `NonEmptyStr`, `Base` из `config/models.py`; `QuerySpec` из `hh_search.config.models`; живая фикстура `tests/fixtures/robots_hh.txt`; `Robots.parse` и `Robots.can_fetch` из `hh_search/sources/http.py`.
- Produces:
  - `class WorkFormat(StrEnum)` со значениями `REMOTE`, `HYBRID`, `ON_SITE`, `FIELD_WORK`
  - `QuerySpec.work_format: WorkFormat | None = None`
  - `build_listing_url(query: QuerySpec, page: int = 0) -> str` — сигнатура не меняется, поведение расширяется

- [ ] **Step 1: Написать падающие тесты формы URL и robots**

В `tests/test_listing.py`:

```python
def test_listing_url_without_work_format_is_unchanged() -> None:
    """Первый поток обязан остаться байт в байт прежним."""
    query = QuerySpec(slug="programmist", cluster="backend")
    assert build_listing_url(query, 0) == "https://hh.ru/vacancies/programmist"
    assert build_listing_url(query, 2) == "https://hh.ru/vacancies/programmist?page=2"


def test_listing_url_with_work_format_always_carries_page() -> None:
    """Первая страница второго потока идёт с `&page=0`, а не голым параметром.

    Без `&page=` правило `Allow: /vacancies/*?*&page=` не срабатывает, и URL
    попадает под `Disallow: *?*` — то есть голый `?work_format=REMOTE` наш
    матчер обязан отвергнуть (сторож ниже это и проверяет).
    """
    query = QuerySpec(slug="programmist", cluster="remote", work_format=WorkFormat.REMOTE)
    assert build_listing_url(query, 0) == (
        "https://hh.ru/vacancies/programmist?work_format=REMOTE&page=0"
    )
    assert build_listing_url(query, 3) == (
        "https://hh.ru/vacancies/programmist?work_format=REMOTE&page=3"
    )


def test_every_generated_url_is_allowed_by_the_live_robots_rules() -> None:
    """Сторож главного требования проекта: в сеть не уходит запрещённый URL.

    Правила берутся с живой фикстуры, а не выдумываются: именно на выдуманных
    правилах прежняя редакция спеки утверждала неверное.
    """
    robots = Robots.parse((FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"))
    plain = QuerySpec(slug="programmist", cluster="backend")
    remote = QuerySpec(slug="programmist", cluster="remote", work_format=WorkFormat.REMOTE)
    for query in (plain, remote):
        for page in (0, 1, 5):
            url = build_listing_url(query, page)
            assert robots.can_fetch("hh-search/0.1", url), url


def test_work_format_without_page_would_be_forbidden() -> None:
    """Премисса предыдущего теста, проверенная явно.

    Если однажды кто-то «упростит» форму до голого `?work_format=REMOTE`,
    этот тест объяснит, почему так нельзя, — вместо того чтобы дать в сеть
    уйти запрещённому URL.
    """
    robots = Robots.parse((FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"))
    assert not robots.can_fetch(
        "hh-search/0.1", "https://hh.ru/vacancies/programmist?work_format=REMOTE"
    )
```

В `tests/test_config.py`:

```python
def test_two_streams_over_one_slug_are_legal() -> None:
    """Один листинг двумя потоками — законный конфиг, а не опечатка.

    Прежний валидатор бил по одному `slug` и отверг бы это. Ключ
    уникальности — пара `(slug, work_format)`.
    """
    config = QueriesConfig.model_validate(
        {
            "queries": [
                {"slug": "programmist", "cluster": "backend"},
                {"slug": "programmist", "cluster": "remote", "work_format": "REMOTE"},
            ]
        }
    )
    assert len(config.queries) == 2


def test_fully_duplicate_pair_is_still_rejected() -> None:
    """Мотивация прежнего валидатора сохраняется дословно: полный повтор
    удваивает запросы к hh.ru молча."""
    with pytest.raises(ValidationError):
        QueriesConfig.model_validate(
            {
                "queries": [
                    {"slug": "programmist", "cluster": "a", "work_format": "REMOTE"},
                    {"slug": "programmist", "cluster": "b", "work_format": "REMOTE"},
                ]
            }
        )


def test_duplicate_slug_without_format_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        QueriesConfig.model_validate(
            {
                "queries": [
                    {"slug": "programmist", "cluster": "a"},
                    {"slug": "programmist", "cluster": "b"},
                ]
            }
        )


def test_unknown_work_format_is_rejected() -> None:
    """Перечисление, а не свободная строка: опечатка обязана падать на старте,
    а не уезжать в query-строку к hh.ru."""
    with pytest.raises(ValidationError):
        QueriesConfig.model_validate(
            {"queries": [{"slug": "programmist", "cluster": "a", "work_format": "УДАЛЁННО"}]}
        )


def test_slug_still_rejects_url_syntax() -> None:
    """Появление законного параметра ничего не разрешает в самом slug."""
    with pytest.raises(ValidationError):
        QueriesConfig.model_validate(
            {"queries": [{"slug": "programmist?area=66", "cluster": "a"}]}
        )
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_listing.py tests/test_config.py -q -k "work_format or two_streams or duplicate_pair"`
Expected: FAIL — `ImportError: cannot import name 'WorkFormat'`

- [ ] **Step 3: Реализовать**

В `config/models.py`:

```python
class WorkFormat(StrEnum):
    """Формат работы в терминах hh.ru — значения их перечисления.

    Именно перечисление, а не свободная строка: значение уезжает в
    query-строку запроса к hh.ru, и опечатка обязана падать на старте, а не
    превращаться в бессмысленный фильтр после похода в сеть. Значения взяты
    с живой страницы вакансии (`workFormatsElement`), а не из документации:
    документации на этот ключ нет.
    """

    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ON_SITE = "ON_SITE"
    FIELD_WORK = "FIELD_WORK"
```

`QuerySpec` получает поле:

```python
    # Не задано — листинг берётся голым путём, как и раньше. Задано — тем же
    # путём с параметром и ОБЯЗАТЕЛЬНЫМ `&page=` (см. build_listing_url:
    # без него URL запрещён robots.txt).
    work_format: WorkFormat | None = None
```

`reject_duplicate_slugs` переписывается на пару. Имя метода и текст ошибки
обновить так, чтобы они говорили про пару, а не про slug, — иначе сообщение
начнёт врать, как уже было с `_sinks()`.

В `sources/listing.py`, `build_listing_url`:

```python
    if query.work_format is None:
        # Голый путь для первой страницы: `?page=0` попал бы под
        # `Disallow: *?*`, не получив защиты от `Allow: /vacancies/*?page=`.
        return base if page == 0 else f"{base}?page={page}"
    # С параметром `&page=` обязателен на ВСЕХ страницах, включая первую:
    # правило `Allow: /vacancies/*?*&page=` требует его наличия, и без него
    # URL запрещён. Проверено матчером на живой фикстуре правил.
    return f"{base}?work_format={query.work_format.value}&page={page}"
```

- [ ] **Step 4: Ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 5: Мутационная проверка**

```bash
export NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
```

Мутация: убрать `&page={page}` из ветки с параметром. Обязан покраснеть
`test_every_generated_url_is_allowed_by_the_live_robots_rules`. Откатить.

- [ ] **Step 6: Коммит**

```bash
git add hh_search/config/models.py hh_search/sources/listing.py tests/test_config.py tests/test_listing.py
git commit -m "feat(discovery): второй поток листинга по формату работы"
```

---

### Task 2: Извлечение формата работы и колонка в базе

**Files:**
- Modify: `hh_search/sources/vacancy_page.py` (или создать `hh_search/sources/work_format.py`, если бюджет §4.3 перерастёт)
- Modify: `hh_search/domain/models.py` (`VacancyDetails`)
- Modify: `hh_search/storage/schema.sql`, `hh_search/storage/migrations.py`, `hh_search/storage/mappers.py`
- Test: `tests/test_vacancy_page.py`, `tests/test_repository.py`

**Interfaces:**
- Consumes: `WorkFormat` из Task 1; фикстуры `tests/fixtures/vacancy.html.gz` (`ON_SITE`) и `vacancy_salary.html.gz` (`REMOTE`); образец `SalaryBlockStats` в том же модуле.
- Produces:
  - `extract_work_formats(html: str) -> frozenset[WorkFormat]` — пустое множество, если блока нет
  - `VacancyDetails.work_formats: frozenset[WorkFormat] = frozenset()`
  - `class WorkFormatBlockStats` с методами `record(formats: frozenset[WorkFormat]) -> None` и `log_summary() -> None`
  - колонка `vacancy.work_formats TEXT` — сериализация отсортированным списком через запятую

**Почему перечисление, а не текст.** На странице есть и то и другое:
`data-qa="work-formats-text"` даёт «Формат работы: удалённо», а встроенное
состояние — `workFormats":[{"workFormatsElement":["REMOTE"]}]`. Берётся второе:
текст локализован и сменится от смены языка интерфейса.

**Почему множество.** Живой пример: Team Lead Go из Москвы предлагает `ON_SITE`,
`REMOTE` и `HYBRID` одновременно. Правило «`REMOTE` среди форматов», а не
«формат равен `REMOTE`»; обратное выкинуло бы вакансию, которая удалёнку
допускает.

- [ ] **Step 1: Написать падающие тесты**

```python
def test_work_formats_read_from_the_on_site_fixture() -> None:
    """Живая фикстура, а не синтетика: в этом проекте все Critical нашлись
    живыми данными."""
    html = gzip.open(FIXTURES / "vacancy.html.gz", "rt", encoding="utf-8").read()
    assert extract_work_formats(html) == frozenset({WorkFormat.ON_SITE})


def test_work_formats_read_from_the_remote_fixture() -> None:
    html = gzip.open(FIXTURES / "vacancy_salary.html.gz", "rt", encoding="utf-8").read()
    assert extract_work_formats(html) == frozenset({WorkFormat.REMOTE})


def test_several_formats_are_all_kept() -> None:
    """Вакансия может предлагать несколько форматов, и REMOTE не должен
    потеряться среди них (живой пример: Team Lead Go, три формата)."""
    html = '&#34;workFormats&#34;:[{&#34;workFormatsElement&#34;:[&#34;ON_SITE&#34;,&#34;REMOTE&#34;,&#34;HYBRID&#34;]}]'
    assert extract_work_formats(html) == frozenset(
        {WorkFormat.ON_SITE, WorkFormat.REMOTE, WorkFormat.HYBRID}
    )


def test_missing_block_gives_empty_set_not_an_error() -> None:
    """Отсутствие блока — не отказ страницы: формат необязателен, а дрейф
    ловится агрегатом, не одной страницей."""
    assert extract_work_formats("<html></html>") == frozenset()


def test_unknown_format_value_is_ignored_and_does_not_crash() -> None:
    """hh.ru может завести новое значение перечисления. Неизвестное
    отбрасывается, известные из того же списка сохраняются."""
    html = '&#34;workFormats&#34;:[{&#34;workFormatsElement&#34;:[&#34;REMOTE&#34;,&#34;TELEPORT&#34;]}]'
    assert extract_work_formats(html) == frozenset({WorkFormat.REMOTE})


def test_block_stats_shout_when_no_page_had_formats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сторож дрейфа: молчит на отдельной пустой странице, кричит на прогоне,
    где не нашлось ни одной. Тот же приём, что у SalaryBlockStats."""
    stats = WorkFormatBlockStats()
    for _ in range(5):
        stats.record(frozenset())
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "ни с одной" in caplog.text


def test_block_stats_stay_quiet_when_some_pages_had_formats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stats = WorkFormatBlockStats()
    stats.record(frozenset({WorkFormat.REMOTE}))
    stats.record(frozenset())
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "ни с одной" not in caplog.text
```

В `tests/test_repository.py` — круговой тест хранения:

Стенд там уже есть — `tests/test_repository.py` открывает `SqliteRepository` на
`tmp_path` и сохраняет вакансии существующими хелперами; используй их, новых не
заводи. Три теста, каждый — существующим стендом:

| Тест | Утверждение |
|---|---|
| `test_work_formats_survive_a_round_trip` | сохранить обогащённую вакансию с `frozenset({ON_SITE, REMOTE})`, прочитать обратно выборкой, которой пользуется отчёт → прочитанное множество РАВНО записанному |
| `test_vacancy_without_work_formats_reads_back_as_empty_set` | записать вакансию, не задав форматов (в базе `NULL` — так лежат 189 собранных до этого изменения) → читается `frozenset()`, а не `None` и не исключение |
| `test_unknown_stored_value_is_ignored_on_read` | вписать в колонку сырым SQL строку `REMOTE,TELEPORT` → читается `frozenset({REMOTE})`, прогон не падает. Порча базы сырым SQL — приём, которым в этом проекте уже находили Critical |

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_vacancy_page.py -q -k work_format`
Expected: FAIL — `ImportError: cannot import name 'extract_work_formats'`

- [ ] **Step 3: Реализовать**

Извлечение — регуляркой по сырому HTML, как `extract_salary`: HTML-парсер в
этом проекте сознательно не используется (§11 основной спеки). Значения внутри
встроенного состояния приходят HTML-экранированными (`&#34;`), поэтому
разэкранировать перед разбором.

Колонка добавляется В КОНЕЦ `ADDED_COLUMNS`:

```python
    ("vacancy", "work_formats", "TEXT"),
```

и в `schema.sql` рядом с `area`, с комментарием, почему `TEXT` со списком через
запятую, а не отдельная таблица: множество из четырёх возможных значений, по
которому не делается выборок, — нормализация здесь дала бы join ради ничего.

Сериализация — отсортированный список через запятую, чтобы одно и то же
множество давало один и тот же текст (иначе круговой тест станет флаки, а
`score_detail` — недетерминированным).

- [ ] **Step 4: Ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`

Отдельно проверить миграцию исполнением на КОПИИ живой базы:

```bash
cp data/state/hh.db /tmp/migrate-check.db
uv run python -c "
import sqlite3
from hh_search.storage.repository import SqliteRepository
r = SqliteRepository('/tmp/migrate-check.db')
c = sqlite3.connect('/tmp/migrate-check.db')
print('колонка на месте:', 'work_formats' in {r[1] for r in c.execute('PRAGMA table_info(vacancy)')})
print('вакансий:', c.execute('select count(*) from vacancy').fetchone()[0])
"
```
Expected: колонка появилась, 189 вакансий на месте, ни одна не потеряна.

- [ ] **Step 5: Мутационная проверка**

Мутация: вернуть `extract_work_formats` только первое значение вместо множества.
Обязан покраснеть `test_several_formats_are_all_kept`. Откатить.

- [ ] **Step 6: Коммит**

```bash
git add hh_search/sources hh_search/domain hh_search/storage tests
git commit -m "feat(sources): формат работы со страницы вакансии и колонка в базе"
```

---

### Task 3: Штраф в скоринге

**Files:**
- Modify: `hh_search/config/models.py` (`LocationConfig`, `ProfileConfig.location`)
- Modify: `hh_search/scoring/keyword.py`
- Test: `tests/test_config.py`, `tests/test_scoring.py`

**Interfaces:**
- Consumes: `WorkFormat` из Task 1; `VacancyDetails.work_formats` из Task 2; `ScoreBreakdown` (поля `title`, `stack`, `responsibilities`, `domain`, `penalty`, `total`, `matched`).
- Produces:
  - `class LocationConfig` — `home_areas: list[NonEmptyStr]` (min_length=1), `penalty_not_remote_elsewhere: float = Field(ge=0, le=100)`
  - `ProfileConfig.location: LocationConfig | None = None` — не задан, штрафа нет вовсе (старые профили продолжают работать)

**Правило, в этом порядке.** Регион в `home_areas` → штрафа нет при любом
формате. Иначе `REMOTE` среди форматов → штрафа нет. Иначе штраф.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_scoring.py` уже имеет стенд: `make_profile(...)` собирает
`ProfileConfig`, `score_for(title, description, company=..., ...)` возвращает
`ScoreBreakdown`. Оба надо расширить необязательными параметрами `location` и
`area`/`work_formats`, не сломав существующие вызовы (у них появятся значения по
умолчанию: `location=None`, `area="Нижний Новгород"`, `work_formats=frozenset()`).

Вспомогательный стенд и первый тест целиком — дальше по этому образцу:

```python
LOCATION = LocationConfig(
    home_areas=["Нижний Новгород", "Дзержинск"], penalty_not_remote_elsewhere=40
)

# Заголовок и описание подобраны так, чтобы БЕЗ штрафа балл был заметно выше
# нуля: иначе штраф не отличить от пола оценки, и тест проходил бы вакуумно.
STRONG_TITLE = "Team Lead backend"
STRONG_BODY = "архитектур, менторинг, код-ревью, c++, kubernetes, kafka, docker, телеком"


def test_home_area_is_not_penalised_whatever_the_format() -> None:
    """Домашний регион побеждает формат: офис в родном городе подходит."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    office_at_home = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Нижний Новгород",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert office_at_home.total == plain.total
```

Остальные тесты задачи, по одной строке на утверждение — каждый строится тем же
`score_for` и сравнивается с `plain` из примера выше:

| Тест | Утверждение |
|---|---|
| `test_second_home_area_also_counts` | `area="Дзержинск"`, `ON_SITE` → `total == plain.total` |
| `test_remote_elsewhere_is_not_penalised` | `area="Москва"`, `REMOTE` → `total == plain.total` |
| `test_office_elsewhere_is_penalised` | `area="Казань"`, `ON_SITE` → `total < plain.total` и разница равна 40 |
| `test_hybrid_elsewhere_is_penalised` | `area="Санкт-Петербург"`, `HYBRID` → штраф есть (живой случай: 69.3 в «Топе») |
| `test_remote_among_several_formats_is_enough` | `area="Москва"`, `{ON_SITE, REMOTE, HYBRID}` → `total == plain.total` |
| `test_unknown_format_is_not_penalised` | `area="Москва"`, `frozenset()` → `total == plain.total` |
| `test_unknown_area_is_not_penalised` | `area=None`, `ON_SITE` → `total == plain.total`, по той же причине, что и неизвестный формат |
| `test_penalty_lands_in_score_detail` | `area="Казань"`, `ON_SITE` → `penalty` вырос на 40 относительно `plain.penalty` |
| `test_score_never_goes_below_zero` | слабый заголовок, `area="Казань"`, `ON_SITE`, штраф 100 → `total == 0.0` |
| `test_profile_without_location_section_scores_as_before` | `location=None`, `area="Казань"`, `ON_SITE` → `total == plain.total` |
| `test_penalty_above_hundred_is_rejected` | `LocationConfig(home_areas=["X"], penalty_not_remote_elsewhere=400)` → `ValidationError` |
| `test_empty_home_areas_is_rejected` | `home_areas=[]` → `ValidationError`: раздел есть, а домашнего региона нет — опечатка, при которой штраф ловит всё |

Ни одного `...` в готовом коде быть не должно.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `NO_COLOR=1 uv run pytest tests/test_scoring.py -q -k "penalis or home_area or remote"`
Expected: FAIL — `LocationConfig` не существует.

- [ ] **Step 3: Реализовать**

Сравнение региона — по точному совпадению строки после нормализации пробелов и
регистра. Не по подстроке: «Нижний Новгород» подстрокой сидит в
«Нижний Новгород и область», но и в чём-нибудь неожиданном, а цена ошибки здесь
— молча непоставленный штраф.

- [ ] **Step 4: Ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`

Плюс проверка на живых данных: посчитать, сколько из 189 собранных вакансий
получили бы штраф, будь у них форматы. Число уходит в отчёт задачи — оно нужно
владельцу для настройки.

- [ ] **Step 5: Мутационная проверка**

Мутация: поменять порядок правил (сперва формат, потом домашний регион).
Обязан покраснеть `test_home_area_is_not_penalised_whatever_the_format`. Откатить.

- [ ] **Step 6: Коммит**

```bash
git add hh_search/config/models.py hh_search/scoring/keyword.py tests
git commit -m "feat(scoring): штраф за неудалённую работу вне домашнего региона"
```

---

### Task 4: Формат работы в трёх отчётах

**Files:**
- Modify: `hh_search/sinks/csv_sink.py` (новая колонка), `hh_search/sinks/markdown_sink.py`, `hh_search/sinks/html_report.py`
- Test: `tests/test_sinks.py`, `tests/test_html_report.py`

**Interfaces:**
- Consumes: `VacancyDetails.work_formats` из Task 2; `COLUMNS` из `csv_sink.py`.
- Produces: колонка `work_formats` в CSV; формат в строке вакансии markdown и HTML.

**Зачем.** Без этого штраф выглядит произволом: читатель увидит балл 30 у
приличной с виду вакансии и не поймёт, что она офисная в Казани.

- [ ] **Step 1: Написать падающие тесты**

Отображение единое для всех трёх приёмников, чтобы отчёты не разошлись в
смысле: `REMOTE` → `удалённо`, `HYBRID` → `гибрид`, `ON_SITE` → `офис`,
`FIELD_WORK` → `разъездной`; несколько форматов — через запятую в том же
порядке, что в сортировке множества; пустое множество → `формат не указан`.
Отображение живёт ОДНОЙ функцией в `hh_search/sinks/text.py` (модуль уже создан
и уже обслуживает двоих), а не тремя копиями.

```python
def test_work_format_labels_cover_every_enum_value() -> None:
    """Новое значение перечисления у hh.ru не должно тихо превращаться в пустое
    место в отчёте: отображение обязано покрывать ВСЕ значения WorkFormat."""
    for value in WorkFormat:
        assert format_work_formats(frozenset({value})), value


def test_empty_formats_are_shown_as_unknown_not_as_office() -> None:
    """Пустое множество — «формат не указан», а не «офис». Иначе отчёт
    утверждает то, чего мы не знаем (и что мы решили не штрафовать)."""
    shown = format_work_formats(frozenset())
    assert "не указан" in shown
    assert "офис" not in shown


def test_several_formats_are_shown_together() -> None:
    shown = format_work_formats(frozenset({WorkFormat.REMOTE, WorkFormat.HYBRID}))
    assert "удалённо" in shown
    assert "гибрид" in shown
```

Плюс по одному тесту на приёмник, каждый — существующим стендом того файла:

| Тест | Утверждение |
|---|---|
| `test_csv_has_a_work_format_column` | `"work_formats" in COLUMNS`, и в записанной строке стоит `удалённо` для вакансии с `REMOTE` |
| `test_markdown_entry_shows_the_work_format` | в строке вакансии «Топа» присутствует `удалённо` |
| `test_html_entry_shows_the_work_format` | то же в HTML-записи, и значение экранировано |

Плюс расширить сторож README `test_readme_lists_the_real_csv_columns`: он
сверяет перечень колонок в README с `COLUMNS` и обязан покраснеть от новой
колонки, пока README не поправлен.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `NO_COLOR=1 uv run pytest tests/test_sinks.py tests/test_html_report.py tests/test_spec_matches_code.py -q -k "work_format or csv_columns"`

- [ ] **Step 3: Реализовать**

- [ ] **Step 4: Ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 5: Коммит**

```bash
git add hh_search/sinks tests
git commit -m "feat(sinks): формат работы виден во всех трёх отчётах"
```

---

### Task 5: Образцы конфигов, README и сторожа документации

**Files:**
- Modify: `config.example/queries.yaml`, `config.example/profile.yaml`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (§7 образцы конфигов — сверяется тестом)
- Test: `tests/test_config_example.py`, `tests/test_spec_matches_code.py`

**Interfaces:**
- Consumes: `WorkFormat`, `LocationConfig`, `COLUMNS`.
- Produces: ничего для последующих задач.

`tests/test_config_example.py` сверяет образцы конфигов с блоками §7 спеки — то
есть образец, README и спека обязаны сойтись, иначе тест покраснеет. Это и есть
причина, по которой задача существует отдельно.

- [ ] **Step 1: Написать падающие сторожа**

```python
def test_readme_explains_both_discovery_streams() -> None:
    """Раздел про два потока обязан называть настоящее имя поля и настоящее
    значение перечисления — оба берутся из кода."""
    section = readme_section("## Два потока discovery", "## ")
    assert "work_format" in section
    assert WorkFormat.REMOTE.value in section


def test_readme_lists_the_location_penalty_field() -> None:
    section = README.read_text(encoding="utf-8")
    assert "penalty_not_remote_elsewhere" in section
    assert "home_areas" in section


def test_example_queries_show_both_streams() -> None:
    """Образец обязан показывать два потока по одному slug — иначе человек не
    узнает, что так можно, и продолжит искать только в своём городе."""
```

- [ ] **Step 2: Запустить и убедиться, что падают**

- [ ] **Step 3: Дописать образцы, README и §7 спеки**

README получает раздел «Два потока discovery» с объяснением, откуда берётся
регион (поддомен по IP, а не параметр — с датой замера), и раздел про штраф.

- [ ] **Step 4: Ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 5: Мутационная проверка**

Испортить имя поля в README — обязан покраснеть ровно один сторож. Откатить.

- [ ] **Step 6: Коммит**

```bash
git add config.example README.md docs tests
git commit -m "docs: два потока discovery и штраф за формат, со сторожами"
```

---

## Приёмка

После Task 5 — живой прогон владельцем:

1. Дописать второй поток в `data/config/queries.yaml` и раздел `location` в `profile.yaml`.
2. `docker compose build && docker compose run --rm hh-search run`
3. Проверить: появились ли вакансии вне Нижнего Новгорода с `REMOTE`; получили ли штраф офисные и гибридные вакансии чужих городов; виден ли формат в отчёте.

Ожидаемая нагрузка: 9 страниц первого потока плюс `pages` второго, плюс страницы
новых вакансий. Потолки `app.limits` не меняются.
