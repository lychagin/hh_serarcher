# Ретенция и устойчивость: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть R-3 (повтор вырожденной страницы листинга), T-2 (обслуживание
приёмников независимо от наличия работы) и R-1 (ручная команда уборки) по спеке
`docs/superpowers/specs/2026-08-01-retention-and-resilience-design.md`.

**Architecture:** Три независимые починки в одной ветке. R-3 добавляет тип отказа
`DegenerateListing` и делит `pipeline/discovery.py` по единице работы. T-2 расширяет
протокол `Sink` методом `maintain()` и зовёт его из `report()` до раннего возврата.
R-1 добавляет два модуля (`storage/retention.py`, `pipeline/cleanup.py`) и команду
CLI `cleanup`; SQL уборки живёт только в слое `storage/`.

**Tech Stack:** Python 3.12, uv, typer, httpx, respx, pytest, mypy --strict, ruff,
SQLite.

## Global Constraints

- **Бюджет 150 строк кода на файл** (непустые, не комментарий, не докстринг). Файл,
  перешедший границу, **делится**, а не получает строку-исключение в §4.3 спеки.
  Действующие исключения уже названы в §4.3 и растут только по числу команд CLI.
- **Ворота перед каждым коммитом:** `./gate` — `ruff check .`,
  `ruff format --check .`, `mypy hh_search tests`, `pytest -q`. Падает на первой
  красной проверке.
- **Комментарии, докстринги и сообщения коммитов — по-русски.**
- **Сторож проверяется интеграционно или не проверяется вовсе.** Модульный тест,
  зовущий метод напрямую, в этом проекте дважды был зелен при мёртвом стороже.
- **Весь SQL живёт в `hh_search/storage/` и только там.** Новый метод хранилища
  сперва попадает в протокол `storage/base.py`, иначе `mypy --strict` отвергнет
  подмену реализации в тестах.
- **Любая дата на входе и выходе БД проходит через `storage/time_utils.py`.**
- **Новый модуль обязан появиться в дереве модулей §4.3 спеки**
  `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` — иначе краснеет
  `test_spec_module_tree_matches_the_package`.
- **Число, обязанное протухать, в документ не пишется** — сторожится список, а не
  число.
- Ветка работы: `retention-and-resilience` (уже создана, спека в ней закоммичена).

---

## Файловая структура

| Файл | Ответственность | Задача |
|---|---|---|
| `hh_search/errors.py` | +`DegenerateListing` — измеренно временный отказ листинга | 1 |
| `hh_search/sources/listing.py` | возбуждает `DegenerateListing` там, где нет `ItemList` | 1 |
| `hh_search/pipeline/listing_pages.py` | **новый**: одна страница листинга целиком, включая повтор | 1 |
| `hh_search/pipeline/discovery.py` | остаётся циклом по листингам и агрегатным сторожем | 1 |
| `hh_search/sinks/base.py` | +`Sink.maintain()` | 2 |
| `hh_search/sinks/csv_sink.py`, `markdown_sink.py` | явные пустые `maintain()` | 2 |
| `hh_search/sinks/telegram_sink.py` | `maintain()` забирает уборку черновиков и довозку | 2 |
| `hh_search/pipeline/reporting.py` | зовёт `maintain()` до раннего возврата | 2 |
| `hh_search/storage/base.py` | +протокол `Housekeeper` | 3 |
| `hh_search/storage/retention.py` | **новый**: весь SQL уборки, обе таблицы | 3 |
| `hh_search/storage/repository.py` | делегирует уборку в `Retention` | 3 |
| `hh_search/pipeline/cleanup.py` | **новый**: план и исполнение уборки, файлы отчётов, горизонт | 4 |
| `hh_search/__main__.py` | команда `cleanup`, предупреждение в `report --since` | 5 |

---

## Task 1: R-3 — повтор вырожденной страницы листинга

**Files:**
- Modify: `hh_search/errors.py`
- Modify: `hh_search/sources/listing.py` (место, где не найден блок `ItemList`)
- Create: `hh_search/pipeline/listing_pages.py`
- Modify: `hh_search/pipeline/discovery.py`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (дерево модулей §4.3)
- Test: `tests/test_pipeline.py`, `tests/test_listing.py`

**Interfaces:**
- Produces: `DegenerateListing(FetchFailed)` в `hh_search/errors.py`;
  `store_page(repo, query, url, response, stats, seen, client, digest) -> None` и
  `DegenerateDigest` (поля `pages: int`, `degenerate: int`, `rescued: int`, метод
  `log_summary() -> None`) в `hh_search/pipeline/listing_pages.py`.
- Consumes: ничего из других задач.

- [ ] **Шаг 1: Написать три падающих теста в `tests/test_pipeline.py`**

Помощник кладётся рядом с `listing_html` (около строки 62):

```python
def listing_without_item_list(slug: str = "programmist") -> str:
    """Вырожденный ответ hh.ru: canonical на месте, блока ItemList нет.

    Ровно то, что hh.ru перемежающеся отдаёт живьём: код 200, вёрстка
    цела, JSON-LD на странице есть — но не тот. Перекачка той же страницы
    проходит, то есть отказ временный (спека 2026-08-01 §1.1).
    """
    return (
        f'<html><head><link rel="canonical" href="https://hh.ru/vacancies/{slug}">'
        '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
        "</head><body></body></html>"
    )
```

Тесты — в конец файла, отдельным разделом:

```python
# --- R-3: вырожденная страница листинга повторяется ровно один раз ---------


@respx.mock
def test_degenerate_listing_is_retried_and_the_run_stays_ok(
    config: Config, repo: SqliteRepository
) -> None:
    """Повтор спасает страницу, и прогон остаётся `ok`.

    Смысл починки именно в статусе: при 12 страницах за прогон
    вырожденный ответ выпадает в трёх прогонах из четырёх, и `partial`
    переставал отличаться от настоящего дрейфа вёрстки.
    """
    mock_robots()
    listing = respx.get(url__startswith=LISTING_URL).mock(
        side_effect=[
            httpx.Response(200, text=listing_without_item_list()),
            httpx.Response(200, text=TWO_VACANCIES),
        ]
    )
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    sink = RecordingSink()

    stats = run(config, repo, [sink])

    assert (stats.status, stats.error) == ("ok", None)
    assert listing.call_count == 2, "ровно один повтор, не больше и не меньше"
    assert sink.seen == ["111"]


@respx.mock
def test_degenerate_listing_twice_degrades_the_run_and_stops_at_two_requests(
    tmp_path: Path, db_path: str
) -> None:
    """Второй вырожденный ответ подряд — `partial`, и третьего запроса нет.

    Листингов два: будь он один, прогон без вакансий понизил бы себя до
    `failed` агрегатным сторожем `_check_not_silent`, и тест проверял бы
    не ту строку.
    """
    root = tmp_path / "two"
    root.mkdir(parents=True, exist_ok=True)
    two_pages = load_config(
        write_config(root, **{"queries.yaml": ONE_PAGE.replace("pages: 1", "pages: 2")})
    )
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_robots()
    listing = respx.get(url__startswith=LISTING_URL).mock(
        side_effect=[
            httpx.Response(200, text=TWO_VACANCIES),
            httpx.Response(200, text=listing_without_item_list()),
            httpx.Response(200, text=listing_without_item_list()),
        ]
    )
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))

    stats = run(two_pages, disk, [RecordingSink()])

    assert stats.status == "partial"
    assert listing.call_count == 3, "первая страница без повтора, вторая с одним"


@respx.mock
def test_a_listing_that_is_not_ours_is_never_retried(
    config: Config, repo: SqliteRepository
) -> None:
    """Промах `canonical` — отказ ПОСТОЯННЫЙ, и повтор только удвоил бы запросы.

    Так выглядит несуществующий slug: hh.ru отвечает 200 и общим
    индексом. Повторять это значило бы удваивать нагрузку на hh.ru на
    каждой странице каждого прогона до тех пор, пока человек не заметит
    опечатку в конфиге.
    """
    mock_robots()
    listing = respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(
            200, text=listing_html(("111", "Senior Embedded Engineer"), slug="yocto")
        )
    )

    run(config, repo, [RecordingSink()])

    assert listing.call_count == 1
```

- [ ] **Шаг 2: Прогнать тесты и убедиться, что они падают**

Run: `uv run pytest tests/test_pipeline.py -k "degenerate or not_ours" -v`
Expected: FAIL — первый на `listing.call_count == 2` (получит 1), второй на
`call_count == 3` (получит 2), третий пройдёт по построению (сторож на то, чтобы
починка его не сломала).

- [ ] **Шаг 3: Добавить тип отказа в `hh_search/errors.py`**

Дописать в конец файла:

```python
class DegenerateListing(FetchFailed):
    """Страница листинга без блока `ItemList`: измеренно ВРЕМЕННЫЙ отказ.

    hh.ru перемежающеся отдаёт код 200, целую вёрстку и страницу без
    блока, из которого берутся id вакансий; перекачка той же страницы
    проходит. Отдельный тип нужен ровно для того, чтобы отличать этот
    отказ от четырёх постоянных, которые тоже возбуждает `parse_listing`.

    Наследник `FetchFailed`, а не самостоятельный класс: все существующие
    обработчики продолжают работать без правок, и место, забывшее про
    новый тип, ведёт себя по-старому, а не падает.
    """
```

- [ ] **Шаг 4: Возбудить его в `hh_search/sources/listing.py`**

В функции, читающей блок `ItemList`, заменить `raise FetchFailed(` на
`raise DegenerateListing(` — **только** в ветке «блока нет» (около строки 198,
текст начинается «на странице листинга ... нет блока JSON-LD с ItemList»).
Остальные четыре `raise FetchFailed` не трогать. Добавить импорт
`DegenerateListing` рядом с `FetchFailed`.

- [ ] **Шаг 5: Создать `hh_search/pipeline/listing_pages.py`**

```python
"""Одна страница листинга целиком: забрать, разобрать, записать (спека
2026-08-01 §1).

Отделено от `discovery.py` по единице работы, а не по слою: там цикл по
листингам и страницам плюс агрегатный сторож «прогон не может быть
пустым», здесь — всё, что происходит с ОДНОЙ страницей, включая повтор
вырожденного ответа. Держать их вместе значило бы файл за границей
бюджета в 150 строк.

Порядок записи прежний и не стилистический: валидатор условного запроса
сохраняется ПОСЛЕ того, как все вакансии страницы оказались в базе.
Обратный порядок оставляет в `http_cache` валидатор снимка, который
никогда не был прочитан.
"""

import logging
from dataclasses import dataclass

import httpx

from hh_search.config.models import QuerySpec
from hh_search.errors import AccessForbidden, DegenerateListing, FetchFailed, RobotsDisallowed
from hh_search.pipeline.stats import PARTIAL, RunStats
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import parse_listing
from hh_search.storage.base import Repository

logger = logging.getLogger(__name__)

OK_STATUS = 200


@dataclass
class DegenerateDigest:
    """Счётчик вырожденных страниц: одна ничего не значит, все — дрейф.

    По одной странице сказать нельзя ничего: вырожденный ответ измерен
    как рядовая помеха частотой ~1 из 9. Но прогон, где повтор
    понадобился КАЖДОЙ отданной странице, означает не помеху, а то, что
    hh.ru перестал отдавать `ItemList` с первого раза, — и такое обязано
    быть слышно. Тот же приём, что у `WorkFormatBlockStats`.
    """

    pages: int = 0
    degenerate: int = 0
    rescued: int = 0

    def log_summary(self) -> None:
        if not self.degenerate:
            return
        if self.degenerate == self.pages:
            logger.warning(
                "все %d отданных страниц листингов пришли без блока ItemList, повтор спас "
                "%d: это уже не рядовая помеха источника, а похоже на смену разметки",
                self.pages,
                self.rescued,
            )
        else:
            logger.info(
                "страниц листингов без блока ItemList: %d из %d, повтор спас %d",
                self.degenerate,
                self.pages,
                self.rescued,
            )


def store_page(
    repo: Repository,
    query: QuerySpec,
    url: str,
    response: httpx.Response,
    stats: RunStats,
    seen: set[str],
    client: PoliteClient,
    digest: DegenerateDigest,
) -> None:
    """Разобрать страницу, записать вакансии и только потом — валидатор."""
    digest.pages += 1
    try:
        vacancies, final = _parse_with_retry(url, response, query.slug, client, digest)
    except FetchFailed as error:
        # Валидатор не сохраняем и вычищаем прежний: 304 на следующем
        # прогоне спрятал бы дрейф формата за нулевой работой, а один
        # лишний полный ответ — дешевле месяца молчания.
        repo.reset_cache(url)
        stats.degrade(PARTIAL, f"листинг {url} не разобран: {error}")
        logger.error("листинг %s не разобран, кэш условного запроса сброшен: %s", url, error)
        return
    for vacancy in vacancies:
        # Запись идёт в любом случае, а счёт — только на первой встрече.
        # Пропускать повтор целиком нельзя: `add_discovered` идемпотентен и
        # именно он решает судьбу кластера (охрана `cluster_weight <`), а
        # `new_count` уже верен — на известный id метод отвечает False.
        if repo.add_discovered(vacancy, query.cluster, query.weight):
            stats.new_count += 1
        if vacancy.id not in seen:
            seen.add(vacancy.id)
            stats.discovered += 1
    # Валидаторы берутся от ТОГО ответа, который разобрался: сохрани мы
    # заголовки вырожденной страницы, следующий прогон получил бы на неё
    # 304 и не увидел бы вакансий вовсе.
    repo.save_cache_headers(url, final.headers.get("ETag"), final.headers.get("Last-Modified"))


def _parse_with_retry(
    url: str,
    response: httpx.Response,
    slug: str,
    client: PoliteClient,
    digest: DegenerateDigest,
) -> tuple[list[object], httpx.Response]:
    """Разбор с одним безусловным повтором на вырожденный ответ.

    Повторяется РОВНО `DegenerateListing` и ничего больше: остальные
    четыре отказа `parse_listing` постоянны (несуществующий slug вернёт то
    же самое) либо означают дрейф разметки, где правильное поведение —
    остановиться и закричать, а не удваивать запросы.

    Повтор безусловный: первый запрос уже получил 200, и `If-None-Match`
    вернул бы `304` с пустым телом. Пауза берётся сама — троттлинг живёт
    в клиенте.
    """
    try:
        return parse_listing(response.text, slug), response
    except DegenerateListing as error:
        digest.degenerate += 1
        logger.warning("листинг %s пришёл без блока ItemList, повторяем запрос: %s", url, error)
    try:
        retry = client.get(url)
    except (AccessForbidden, FetchFailed, RobotsDisallowed) as error:
        # Отдельной ветки у «повтор упал сам» нет намеренно: с точки
        # зрения прогона это тот же потерянный листинг, и различать их
        # значило бы плодить состояния без разных последствий. 403 при
        # этом НЕ пробрасывается наружу: устойчивую серию считает
        # `ForbiddenStreak` по первым запросам, а повтор — наша
        # инициатива, и обрывать ею прогон нечестно.
        raise FetchFailed(f"повторный запрос не удался: {error}") from error
    if retry.status_code != OK_STATUS:
        raise FetchFailed(f"повторный запрос вернул код {retry.status_code}")
    vacancies = parse_listing(retry.text, slug)
    digest.rescued += 1
    return vacancies, retry
```

Тип `list[object]` в возврате `_parse_with_retry` подставить настоящим: это то,
что возвращает `parse_listing` (`list[DiscoveredVacancy]`), импорт из
`hh_search.domain.models`.

- [ ] **Шаг 6: Убрать `_store_page` из `discovery.py` и подключить новый модуль**

В `hh_search/pipeline/discovery.py`:

1. удалить функцию `_store_page` целиком (строки 93–125);
2. добавить импорт `from hh_search.pipeline.listing_pages import DegenerateDigest, store_page`;
3. убрать ставшие лишними импорты (`httpx`, `parse_listing`, `QuerySpec` — если
   больше не используются; `ruff` покажет);
4. в `discover` завести дайджест и передавать его:

```python
    skipped = FailureDigest()
    degenerate = DegenerateDigest()
```

```python
            fetched += 1
            store_page(repo, query, url, response, stats, seen, client, degenerate)
    skipped.log_summary("страниц листингов не получено")
    degenerate.log_summary()
    _check_not_silent(config, stats, fetched, unchanged)
```

- [ ] **Шаг 7: Прогнать тесты задачи**

Run: `uv run pytest tests/test_pipeline.py -k "degenerate or not_ours" -v`
Expected: PASS, все три.

- [ ] **Шаг 8: Добавить модульный сторож типа отказа в `tests/test_listing.py`**

```python
def test_missing_item_list_raises_the_retryable_type() -> None:
    """Тип отказа — часть контракта: по нему конвейер решает, повторять ли.

    Проверяется вместе с обратным случаем: промах `canonical` обязан
    остаться обычным `FetchFailed`, иначе повтор распространится на
    постоянный отказ.
    """
    without_block = (
        '<html><head><link rel="canonical" href="https://hh.ru/vacancies/programmist">'
        '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
        "</head></html>"
    )
    with pytest.raises(DegenerateListing):
        parse_listing(without_block, "programmist")

    wrong_slug = without_block.replace("programmist", "yocto", 1)
    with pytest.raises(FetchFailed) as caught:
        parse_listing(wrong_slug, "programmist")
    assert not isinstance(caught.value, DegenerateListing)
```

- [ ] **Шаг 9: Обновить дерево модулей §4.3 спеки**

В `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md`, в дереве модулей
(около строки 369), рядом со строкой `discovery.py` добавить:

```
    listing_pages.py      одна страница листинга: разбор, повтор вырожденного ответа, запись
```

- [ ] **Шаг 10: Прогнать ворота целиком**

Run: `./gate`
Expected: всё зелёное. Если `test_spec_module_tree_matches_the_package` красный —
строка в дереве не совпала с именем файла.

- [ ] **Шаг 11: Коммит**

```bash
git add hh_search/errors.py hh_search/sources/listing.py hh_search/pipeline/listing_pages.py \
        hh_search/pipeline/discovery.py tests/test_pipeline.py tests/test_listing.py \
        docs/superpowers/specs/2026-07-27-hh-autosearch-design.md
git commit -m "fix: вырожденная страница листинга повторяется один раз (R-3)"
```

---

## Task 2: T-2 — обслуживание приёмников независимо от наличия работы

**Files:**
- Modify: `hh_search/sinks/base.py`
- Modify: `hh_search/sinks/csv_sink.py`, `hh_search/sinks/markdown_sink.py`
- Modify: `hh_search/sinks/telegram_sink.py`
- Modify: `hh_search/pipeline/reporting.py`
- Test: `tests/test_pipeline.py`, `tests/test_telegram_sink.py`

**Interfaces:**
- Produces: `Sink.maintain(now: datetime) -> None` в протоколе
  `hh_search/sinks/base.py`; `maintain_sinks(sinks: Sequence[Sink], moment: datetime) -> None`
  в `hh_search/pipeline/reporting.py`.
- Consumes: ничего из Task 1.

- [ ] **Шаг 1: Написать падающие интеграционные тесты в `tests/test_pipeline.py`**

```python
# --- T-2: обслуживание приёмника не зависит от наличия работы --------------


@respx.mock
def test_a_run_with_nothing_to_report_still_redelivers_a_stuck_document(
    config: Config, repo: SqliteRepository, tmp_path: Path
) -> None:
    """Прогон, которому отправлять нечего, чинит вчерашний застрявший документ.

    До починки этот путь был недостижим по построению: `report()`
    возвращался при пустой очереди, не позвав ни одного приёмника, а
    довозка жила внутри `emit`, за веткой «вакансии пришли, но все
    дубли». Тихие сутки — лучшее время чинить застрявшее, а были
    единственным временем не чинить.
    """
    mock_source(listing_html(("111", "Senior Embedded Engineer")))
    client = FakeClient()
    telegram = TelegramSink(tmp_path, config.profile.report_threshold, client)  # type: ignore[arg-type]

    run(config, repo, [telegram])
    assert [name for name, _, _ in client.documents] == ["2026-07-28-new.html"]

    stuck = tmp_path / "2026-07-27-new.html"
    stuck.write_text("<html>вчерашний отчёт</html>", encoding="utf-8")

    second = run(config, repo, [telegram])

    assert (second.status, second.reported) == ("ok", 0)
    assert [name for name, _, _ in client.documents] == [
        "2026-07-28-new.html",
        "2026-07-27-new.html",
    ]


@respx.mock
def test_the_stuck_document_arrives_before_todays(
    config: Config, repo: SqliteRepository, tmp_path: Path
) -> None:
    """Довозка идёт ПЕРЕД отправкой сегодняшнего, а не после.

    Иначе документы легли бы в канале задом наперёд: сначала сегодняшний,
    потом вчерашний.
    """
    mock_source(listing_html(("111", "Senior Embedded Engineer")))
    client = FakeClient()
    telegram = TelegramSink(tmp_path, config.profile.report_threshold, client)  # type: ignore[arg-type]
    stuck = tmp_path / "2026-07-27-new.html"
    stuck.write_text("<html>вчерашний отчёт</html>", encoding="utf-8")

    run(config, repo, [telegram])

    assert [name for name, _, _ in client.documents] == [
        "2026-07-27-new.html",
        "2026-07-28-new.html",
    ]


@respx.mock
def test_a_failing_maintain_does_not_degrade_the_run(
    config: Config, repo: SqliteRepository, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Недоступный Telegram не красит прогон, которому и отправлять нечего.

    Понижение до `partial` означало бы, что отказ транспорта красит каждый
    прогон подряд, — ровно та болезнь, от которой R-3 лечит `partial`.
    Самоизлечение остаётся: отсутствующая отметка о доставке — факт на
    диске, и следующий прогон попробует снова.
    """
    mock_source(listing_html(("111", "Senior Embedded Engineer")))
    healthy = TelegramSink(tmp_path, config.profile.report_threshold, FakeClient())  # type: ignore[arg-type]
    run(config, repo, [healthy])

    stuck = tmp_path / "2026-07-27-new.html"
    stuck.write_text("<html>вчерашний отчёт</html>", encoding="utf-8")
    broken = FakeClient(fail_on="sendDocument")
    sink = TelegramSink(tmp_path, config.profile.report_threshold, broken)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [sink])

    assert (stats.status, stats.error) == ("ok", None)
    assert "telegram" in caplog.text
    assert not (tmp_path / "2026-07-27-new.html.sent").exists(), (
        "отметка не ставится при отказе — иначе документ считался бы доставленным"
    )
```

- [ ] **Шаг 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_pipeline.py -k "stuck_document or maintain" -v`
Expected: FAIL — первый и третий на том, что `client.documents` содержит только
сегодняшний документ; второй на порядке.

- [ ] **Шаг 3: Расширить протокол `Sink`**

В `hh_search/sinks/base.py` дописать в класс `Sink`:

```python
    def maintain(self, now: datetime) -> None:
        """Обслужить приёмник — независимо от того, есть ли что отправлять.

        Зовётся `report()` ДО раннего возврата при пустой очереди, то есть
        и в прогонах, где отправлять нечего. Сюда попадает работа, которую
        нельзя привязывать к наличию вакансий: у `telegram` это уборка
        черновиков и довозка документов, застрявших без отметки о
        доставке.

        Отказ отсюда не понижает статус прогона: недоступный транспорт
        красил бы `partial` каждый прогон подряд, а самоизлечение и так на
        месте — признак застрявшего документа лежит на диске.
        """
        ...
```

- [ ] **Шаг 4: Добавить явные пустые реализации файловым приёмникам**

В `hh_search/sinks/csv_sink.py` и `hh_search/sinks/markdown_sink.py` — в каждый
класс:

```python
    def maintain(self, now: datetime) -> None:
        """Обслуживать нечего: файл дня пишется целиком в `emit`."""
```

Явная заглушка, а не проверка `hasattr` у вызывающего: протокол в Python
структурный, наследования нет, и заглушку видит и mypy, и человек, который будет
писать четвёртый приёмник.

- [ ] **Шаг 5: Перенести обслуживание в `TelegramSink.maintain`**

В `hh_search/sinks/telegram_sink.py`:

```python
    def maintain(self, now: datetime) -> None:
        """Уборка черновиков и довозка застрявших документов (спека §5, item 1).

        Обе вещи жили внутри `emit` и потому не случались там, где нужнее
        всего: в прогоне, которому отправлять нечего. `emit` при пустом
        списке вакансий возвращается раньше, а `report()` при пустой
        очереди не зовёт приёмников вовсе, — то есть тихие сутки не чинили
        ничего, хотя именно в них демон свободен.
        """
        self._sweep_orphaned_drafts()
        self._redeliver(self._previous_days(now.date()))
```

В `emit` убрать первую строку `self._sweep_orphaned_drafts()` и заменить ветку

```python
        if not fresh:
            return self._redeliver(previous)
```

на

```python
        if not fresh:
            # Довозка отсюда ушла в `maintain`: она к «все вакансии
            # оказались дублями» отношения не имеет и запускалась этим
            # признаком случайно. Ноль здесь верен буквально — записано
            # ничего не было.
            return 0
```

Сменить сигнатуру `_redeliver` на `-> None` (её результат больше никому не нужен)
и убрать из неё `return 0`.

- [ ] **Шаг 6: Звать `maintain` из `report()`**

В `hh_search/pipeline/reporting.py` добавить функцию и вызов:

```python
def maintain_sinks(sinks: Sequence[Sink], moment: datetime) -> None:
    """Дать каждому приёмнику обслужиться. Отказ громкий, но не заразный.

    Статус прогона не понижается сознательно: недоступный Telegram иначе
    красил бы `partial` каждые четыре часа, обесценивая статус ровно так
    же, как это делала вырожденная страница листинга до R-3. Потеря при
    отказе ограничена: признак застрявшего документа — файл на диске, и
    следующий прогон попробует снова.
    """
    for sink in sinks:
        try:
            sink.maintain(moment)
        except Exception as error:  # noqa: BLE001 — обслуживание не роняет прогон
            logger.error(
                "приёмник %s не обслужен: %s. Статус прогона не понижен, "
                "следующий прогон попробует снова",
                sink.name,
                error,
                exc_info=True,
            )
```

В `report()` — первой строкой, до `_collect`:

```python
def report(...) -> None:
    # ДО раннего возврата при пустой очереди и до `emit`: обслуживание не
    # зависит от наличия работы (T-2), а довезённый вчерашний документ
    # обязан лечь в канале раньше сегодняшнего.
    maintain_sinks(sinks, moment)
    ready = _collect(repo, scorer, stats, limit)
    if not ready:
        return
```

- [ ] **Шаг 7: Дать `maintain` тестовому двойнику `RecordingSink`**

В `tests/test_pipeline.py`, в класс `RecordingSink` (около строки 96):

```python
    def maintain(self, now: datetime) -> None:
        self.maintained += 1
```

и `self.maintained = 0` в `__init__`.

- [ ] **Шаг 8: Прогнать тесты задачи**

Run: `uv run pytest tests/test_pipeline.py tests/test_telegram_sink.py -v`
Expected: PASS. Существующий
`test_telegram_redelivers_the_stuck_document_exactly_once` обязан остаться
зелёным — довозка переехала, но не изменилась.

- [ ] **Шаг 9: Добавить сторож пустой реализации в `tests/test_sinks.py`**

```python
def test_file_sinks_maintain_without_touching_the_disk(tmp_path: Path) -> None:
    """Пустой `maintain` обязан остаться пустым: он зовётся каждый прогон.

    Сторож на случай, если однажды в него положат работу «заодно»:
    `report()` зовёт его в том числе тогда, когда отправлять нечего, и
    запись на диск оттуда была бы работой без повода.
    """
    before = sorted(path.name for path in tmp_path.iterdir())
    CsvSink(tmp_path).maintain(NOW)
    MarkdownSink(tmp_path, 60.0).maintain(NOW)
    assert sorted(path.name for path in tmp_path.iterdir()) == before
```

- [ ] **Шаг 10: Ворота и коммит**

Run: `./gate`

```bash
git add hh_search/sinks/base.py hh_search/sinks/csv_sink.py hh_search/sinks/markdown_sink.py \
        hh_search/sinks/telegram_sink.py hh_search/pipeline/reporting.py \
        tests/test_pipeline.py tests/test_sinks.py
git commit -m "fix: обслуживание приёмников не зависит от наличия работы (T-2)"
```

---

## Task 3: R-1a — `storage/retention.py`, весь SQL уборки

**Files:**
- Create: `hh_search/storage/retention.py`
- Modify: `hh_search/storage/base.py` (протокол `Housekeeper`)
- Modify: `hh_search/storage/repository.py` (композиция и делегирование)
- Modify: `hh_search/storage/CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (дерево модулей)
- Test: `tests/test_repository.py`

**Interfaces:**
- Produces: протокол `Housekeeper` в `hh_search/storage/base.py` с методами
  `descriptions_before(cutoff: datetime) -> tuple[int, int]`,
  `forget_descriptions(cutoff: datetime) -> int`,
  `count_runs_before(cutoff: datetime) -> int`,
  `forget_runs(cutoff: datetime) -> int`,
  `vacuum() -> None`. Класс `Retention` в `hh_search/storage/retention.py`,
  те же пять методов; `SqliteRepository` их делегирует.
- Consumes: ничего.

**Замечание к спеке:** §3.1 спеки называет границей строк журнала `started_at`.
Здесь используется `finished_at` — незакрытая строка (`running`, оставшаяся от
убитого процесса) не имеет даты завершения, и удалять её по дате старта значило бы
стирать улику ровно того отказа, ради которого журнал ведётся. Шаг 8 правит спеку.

- [ ] **Шаг 1: Написать падающие тесты в `tests/test_repository.py`**

```python
# --- R-1: уборка — единственное место, где строки исчезают -----------------


def test_forget_descriptions_only_touches_old_reported_rows(repo: SqliteRepository) -> None:
    """Обнуляются описания отправленных и старых. Всё остальное цело.

    Три соседа проверяются вместе, потому что каждый ломается по-своему:
    свежая отправленная теряется из `report --since` раньше срока,
    строка `new` с описанием выпадает из очереди отчёта, а строка `new`
    без описания ушла бы в сеть за уже скачанной страницей.
    """
    ...  # разложить четыре строки: reported старая, reported свежая, new с описанием, new без
    freed = repo.forget_descriptions(datetime(2026, 5, 1, tzinfo=UTC))
    assert freed == 1


def test_forget_descriptions_is_idempotent(repo: SqliteRepository) -> None:
    """Повторный вызов возвращает 0, а не число уже пустых строк.

    Иначе вывод команды врал бы человеку: «убрано 152» на второй прогон
    подряд означало бы, что уборка что-то делает, хотя делать ей нечего.
    """
    ...
    assert repo.forget_descriptions(cutoff) == 1
    assert repo.forget_descriptions(cutoff) == 0


def test_a_cleaned_vacancy_is_never_fetched_again(repo: SqliteRepository) -> None:
    """Обнулённое описание не возвращает вакансию в очередь обогащения.

    Самый дорогой из инвариантов уборки: очередь отбирает
    `status='new' AND description IS NULL`, и промах здесь означал бы
    повторный запрос к hh.ru за каждой убранной вакансией плюс повторную
    отправку в Telegram.
    """
    ...
    repo.forget_descriptions(cutoff)
    assert repo.pending_enrichment(3, 100) == []


def test_forget_runs_keeps_the_row_that_never_finished(repo: SqliteRepository) -> None:
    """Незакрытая строка журнала переживает уборку.

    `running` без `finished_at` — след убитого процесса, то есть улика
    отказа, ради которой журнал и ведётся. Закроет её
    `close_abandoned_runs()`, и удалит уже следующая уборка.
    """
    ...


def test_descriptions_before_counts_bytes_not_characters(repo: SqliteRepository) -> None:
    """Байты, а не символы: описания кириллические, разница вдвое.

    Число уезжает человеку в вывод команды как «освободится N МБ», и
    ошибка вдвое сделала бы его бесполезным.
    """
    repo.save_description("1", details(description="ЖЖЖ"))
    ...
    rows, size = repo.descriptions_before(cutoff)
    assert (rows, size) == (1, 6)


def test_vacuum_shrinks_the_file_after_descriptions_are_cleared(tmp_path: Path) -> None:
    """Без VACUUM файл не ужимается вовсе — и уборка выглядит сломанной.

    Проверяется на ФАЙЛОВОЙ базе: у `:memory:` размера нет, и сторож был
    бы зелен вакуумно.
    """
    path = tmp_path / "hh.db"
    disk = SqliteRepository(path)
    disk.init_schema()
    ...  # 200 отправленных вакансий с описанием по 4 КБ
    before = path.stat().st_size
    disk.forget_descriptions(cutoff)
    after_update = path.stat().st_size
    disk.vacuum()
    after_vacuum = path.stat().st_size
    assert after_update >= before, "UPDATE ... = NULL сам по себе файл не ужимает"
    assert after_vacuum < before
```

Каждое `...` разложить по образцу соседних тестов файла: они уже умеют раскладывать
строки через `add_discovered` / `save_enriched` / `mark_reported` и портить базу
сырым SQL там, где через API нужного состояния не получить (`reported_at` в
прошлом ставится сырым `UPDATE`).

- [ ] **Шаг 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_repository.py -k "forget or vacuum or descriptions_before" -v`
Expected: FAIL с `AttributeError: 'SqliteRepository' object has no attribute
'forget_descriptions'`.

- [ ] **Шаг 3: Создать `hh_search/storage/retention.py`**

```python
"""Уборка: единственное место в проекте, где строки исчезают.

Обе таблицы обслуживаются здесь, вопреки разделению «`vacancy` в
`repository.py`, `run` в `run_log.py`». Причина названа в спеке
2026-08-01 §3.4: вопрос «что и когда исчезает» обязан читаться в ОДНОМ
файле. Разложенный по двум, он отвечался бы наполовину, и половину при
правке забывали бы.

Соединением модуль не владеет и не закрывает его — как `RunLog`.
"""

import sqlite3
from datetime import datetime

from hh_search.storage.base import STATUS_REPORTED
from hh_search.storage.time_utils import to_utc_iso


class Retention:
    """Уборка старых данных. Ручная: демон эти методы не зовёт."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def descriptions_before(self, cutoff: datetime) -> tuple[int, int]:
        """Сколько описаний старше границы и сколько в них БАЙТ.

        `LENGTH(CAST(... AS BLOB))`, а не `LENGTH(...)`: у TEXT-значения
        `LENGTH` считает символы, и на кириллическом описании число вышло
        бы вдвое меньше правды. Уезжает оно человеку как «освободится
        N МБ», то есть ошибка вдвое делает его бесполезным.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(CAST(description AS BLOB))), 0) AS size "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        ).fetchone()
        return (int(row["rows"]), int(row["size"])) if row else (0, 0)

    def forget_descriptions(self, cutoff: datetime) -> int:
        """Обнулить описания отправленных вакансий старше границы.

        Три условия, и каждое несёт свой инвариант. `status='reported'` —
        очередь обогащения отбирает `status='new' AND description IS
        NULL`, и обнули мы описание у `new`, вакансия ушла бы в сеть за
        уже скачанной страницей. `description IS NOT NULL` — делает метод
        идемпотентным: повторный вызов вернёт 0, а не число уже пустых
        строк. `reported_at < ?` — строки с пустым `reported_at` не
        попадают под сравнение с NULL и остаются целы, что верно: дату
        отправки ставит `mark_reported`, и её отсутствие означает
        состояние, которого уборка не понимает.

        Строка остаётся на месте. Она и есть дедупликация: удалённая
        вакансия была бы найдена заново, скачана ещё раз и повторно
        отправлена в Telegram — циклически, пока висит объявление.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET description = NULL "
            "WHERE status = ? AND description IS NOT NULL AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        )
        self._connection.commit()
        return cursor.rowcount

    def count_runs_before(self, cutoff: datetime) -> int:
        """Сколько ЗАКРЫТЫХ строк журнала старше границы."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS rows FROM run WHERE finished_at IS NOT NULL AND finished_at < ?",
            (to_utc_iso(cutoff),),
        ).fetchone()
        return int(row["rows"]) if row else 0

    def forget_runs(self, cutoff: datetime) -> int:
        """Удалить закрытые строки журнала старше границы.

        По `finished_at`, а не по `started_at`: незакрытая строка
        (`running`, оставшаяся от убитого процесса) даты завершения не
        имеет, и удалять её по дате старта значило бы стирать улику ровно
        того отказа, ради которого журнал ведётся. Такие строки закрывает
        `close_abandoned_runs()`, и удалит их следующая уборка.
        """
        cursor = self._connection.execute(
            "DELETE FROM run WHERE finished_at IS NOT NULL AND finished_at < ?",
            (to_utc_iso(cutoff),),
        )
        self._connection.commit()
        return cursor.rowcount

    def vacuum(self) -> None:
        """Ужать файл базы. Без него уборка не даёт ни одного байта.

        SQLite при `UPDATE ... = NULL` помечает страницы свободными и
        оставляет их в файле. Владелец, посмотрев на размер `hh.db` после
        уборки и увидев то же число, решил бы, что команда сломана.

        `commit()` перед вызовом обязателен: `VACUUM` не выполняется
        внутри открытой транзакции. Цена названа в выводе команды —
        база переписывается целиком и на время держит на диске две копии.
        """
        self._connection.commit()
        self._connection.execute("VACUUM")
```

- [ ] **Шаг 4: Объявить протокол `Housekeeper` в `hh_search/storage/base.py`**

Дописать после класса `Repository`:

```python
class Housekeeper(Protocol):
    """Хранилище в том объёме, в каком его знает УБОРКА (спека 2026-08-01 §3).

    Протокол отдельный, а не пять методов, дописанных в `Repository`, по
    двум причинам. Уборка не участвует в прогоне: `run_once` не зовёт ни
    одного из этих методов, и класть их в протокол, чей докстринг
    обещает «хранилище в том объёме, в каком его знает конвейер», значило
    бы соврать в этом обещании. И практически: `Repository` реализует
    тестовый двойник целиком в `dict`, и он рос бы методами, которых
    никогда не вызовет.
    """

    def descriptions_before(self, cutoff: datetime) -> tuple[int, int]:
        """Сколько описаний старше границы и сколько в них байт."""
        ...

    def forget_descriptions(self, cutoff: datetime) -> int:
        """Обнулить их. Возвращает число тронутых строк."""
        ...

    def count_runs_before(self, cutoff: datetime) -> int:
        """Сколько закрытых строк журнала старше границы."""
        ...

    def forget_runs(self, cutoff: datetime) -> int:
        """Удалить их. Возвращает число удалённых строк."""
        ...

    def vacuum(self) -> None:
        """Ужать файл базы — иначе уборка не освобождает ни байта."""
        ...
```

- [ ] **Шаг 5: Подключить `Retention` к `SqliteRepository`**

В `hh_search/storage/repository.py`:

```python
from hh_search.storage.retention import Retention
```

в `__init__`, рядом с `self._run_log`:

```python
        self._retention = Retention(self._connection)
```

и в конец класса, отдельным разделом:

```python
    # --- уборка: делегируется в Retention -------------------------------

    def descriptions_before(self, cutoff: datetime) -> tuple[int, int]:
        return self._retention.descriptions_before(cutoff)

    def forget_descriptions(self, cutoff: datetime) -> int:
        return self._retention.forget_descriptions(cutoff)

    def count_runs_before(self, cutoff: datetime) -> int:
        return self._retention.count_runs_before(cutoff)

    def forget_runs(self, cutoff: datetime) -> int:
        return self._retention.forget_runs(cutoff)

    def vacuum(self) -> None:
        self._retention.vacuum()
```

- [ ] **Шаг 6: Прогнать тесты задачи**

Run: `uv run pytest tests/test_repository.py -k "forget or vacuum or descriptions_before or fetched_again" -v`
Expected: PASS.

- [ ] **Шаг 7: Добавить сторож соответствия протоколу**

В `tests/test_repository.py`:

```python
def _as_housekeeper(repo: Housekeeper) -> Housekeeper:
    """Совместимость с протоколом уборки, зафиксированная для `mypy --strict`."""
    return repo


def test_sqlite_repository_satisfies_the_housekeeper_protocol(repo: SqliteRepository) -> None:
    """Доказательство — пара «тест зелёный» и «файл проходит mypy --strict».

    Рантайм здесь не доказывает ничего: протоколы структурные, и
    несовпадение сигнатуры видит только проверка типов.
    """
    assert _as_housekeeper(repo) is repo
```

- [ ] **Шаг 8: Обновить документы**

1. Дерево модулей §4.3 спеки `2026-07-27-hh-autosearch-design.md`, рядом с
   `run_log.py`:
   ```
       retention.py          уборка: обнуление описаний, удаление строк журнала, VACUUM
   ```
2. `hh_search/storage/CLAUDE.md`, в строку про распределение таблиц, дописать:
   ```
   - **Уборка — исключение из этого распределения.** `retention.py` трогает и
     `vacancy`, и `run`: «что и когда исчезает» обязано читаться в одном файле.
   ```
3. Спека `2026-08-01-retention-and-resilience-design.md`, §3.1: заменить
   «365 дней от `started_at`» на «365 дней от `finished_at`» и добавить причину
   («незакрытая строка — улика отказа, даты завершения у неё нет»).

- [ ] **Шаг 9: Ворота и коммит**

Run: `./gate`

```bash
git add hh_search/storage/retention.py hh_search/storage/base.py \
        hh_search/storage/repository.py hh_search/storage/CLAUDE.md \
        tests/test_repository.py docs/superpowers/specs/
git commit -m "feat: SQL уборки — обнуление описаний, чистка журнала, VACUUM (R-1)"
```

---

## Task 4: R-1b — `pipeline/cleanup.py`, план и исполнение

**Files:**
- Create: `hh_search/pipeline/cleanup.py`
- Create: `tests/test_cleanup.py`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (дерево модулей)

**Interfaces:**
- Consumes: протокол `Housekeeper` из Task 3; `LOOKBACK_DAYS` из
  `hh_search/sinks/telegram_sink.py`.
- Produces: `CleanupDays` (поля `descriptions: int = 90`, `runs: int = 365`,
  `reports: int | None = None`), `CleanupPlan` (поля `descriptions: int`,
  `description_bytes: int`, `runs: int`, `report_files: int`, `report_bytes: int`,
  `descriptions_cutoff: datetime`, метод `describe(applied: bool) -> str`),
  `PROTECTED_DAYS: int`, `plan(...) -> CleanupPlan`, `execute(...) -> CleanupPlan`,
  `horizon(state_dir: Path) -> date | None`.

- [ ] **Шаг 1: Написать падающие тесты в новом `tests/test_cleanup.py`**

```python
"""Уборка: план, исполнение и то, чего она не трогает никогда."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hh_search.pipeline.cleanup import (
    PROTECTED_DAYS,
    CleanupDays,
    execute,
    horizon,
    plan,
)
from hh_search.sinks.telegram_sink import LOOKBACK_DAYS
from hh_search.storage.repository import SqliteRepository

NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def report_file(reports: Path, day: date, suffix: str = "-new.html") -> Path:
    path = reports / f"{day:%Y-%m-%d}{suffix}"
    path.write_text("<html>отчёт</html>", encoding="utf-8")
    return path


def test_plan_changes_nothing_on_disk(tmp_path: Path) -> None:
    """Сухой прогон обязан быть сухим — сверка побайтно.

    Для необратимой команды это единственный сторож, который отличает
    «показал план» от «сделал и рассказал».
    """
    ...
    before = {path: path.read_bytes() for path in sorted(reports.iterdir())}
    db_before = db.read_bytes()

    result = plan(repo, reports, NOW, CleanupDays(reports=90))

    assert result.descriptions == 1
    assert {path: path.read_bytes() for path in sorted(reports.iterdir())} == before
    assert db.read_bytes() == db_before


def test_execute_clears_descriptions_and_removes_old_report_files(tmp_path: Path) -> None:
    """Исполнение делает ровно то, что обещал план."""
    ...


def test_report_files_are_kept_without_the_reports_flag(tmp_path: Path) -> None:
    """Без флага файлы целы: удалённый отчёт не восстановит ничто.

    Описание всегда можно перекачать с hh.ru, строку журнала не жалко, а
    файл — единственное необратимое из трёх, и оно не должно случаться
    заодно с двумя обратимыми.
    """
    ...
    execute(repo, reports, state, NOW, CleanupDays(reports=None))
    assert (reports / "2020-01-01-new.html").exists()


def test_the_redelivery_window_survives_reports_days_zero(tmp_path: Path) -> None:
    """Файлы дня и отметки за окно довозки не удаляются даже при сроке 0.

    Удалённая отметка `.sent` означает «документ застрял», и следующий
    прогон отправил бы его в канал повторно. Сторож привязан к
    `LOOKBACK_DAYS`, а не к числу: разъедься они — и уборка начала бы
    ломать довозку молча.
    """
    assert PROTECTED_DAYS >= LOOKBACK_DAYS + 1
    reports = tmp_path / "reports"
    reports.mkdir()
    for offset in range(LOOKBACK_DAYS + 1):
        day = NOW.date() - timedelta(days=offset)
        report_file(reports, day)
        report_file(reports, day, suffix="-new.html.sent")
    ancient = report_file(reports, date(2020, 1, 1))

    execute(repo, reports, state, NOW, CleanupDays(reports=0))

    assert not ancient.exists()
    assert len(sorted(reports.iterdir())) == 2 * (LOOKBACK_DAYS + 1)


def test_files_without_a_date_prefix_are_never_touched(tmp_path: Path) -> None:
    """Чужие файлы и черновики уборка не трогает.

    За черновики `*.part` отвечает `TelegramSink.maintain`. Два
    механизма, убирающие одни и те же файлы, однажды разойдутся в том,
    кто из них главный.
    """
    ...
    draft = reports / "2020-01-01-new.htmlABC.part"
    ...
    assert draft.exists()
    assert (reports / "README.txt").exists()


def test_horizon_is_written_by_execute_and_read_back(tmp_path: Path) -> None:
    """Горизонт уборки — факт на диске, а не догадка.

    Без него потеря была бы тихой: выборки отчёта фильтруют по
    `description IS NOT NULL`, и `report --since 120d` после уборки на 90
    днях молча показал бы 90.
    """
    ...
    execute(repo, reports, state, NOW, CleanupDays(descriptions=90))
    assert horizon(state) == date(2026, 5, 3)


def test_an_unreadable_horizon_reads_as_absent(tmp_path: Path) -> None:
    """Мусор в файле горизонта не роняет `report`.

    Предупреждение, которое роняет команду, хуже отсутствующего
    предупреждения.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "last-cleanup").write_text("не дата", encoding="utf-8")
    assert horizon(state) is None
```

Все `...` разложить: база создаётся `SqliteRepository(tmp_path / "hh.db")` с
`init_schema()`, отправленные вакансии — тем же приёмом, что в
`tests/test_repository.py`.

- [ ] **Шаг 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_cleanup.py -v`
Expected: FAIL с `ModuleNotFoundError: hh_search.pipeline.cleanup`.

- [ ] **Шаг 3: Создать `hh_search/pipeline/cleanup.py`**

```python
"""Ручная уборка: план и исполнение (спека 2026-08-01 §3).

Оркестровка и только она. SQL живёт в `storage/retention.py`, а знание о
том, каких файлов отчётов касаться нельзя, взято из
`sinks/telegram_sink.py` ИМПОРТОМ константы, а не переписанным числом:
разъедься они — и уборка начала бы ломать довозку молча.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.sinks.telegram_sink import LOOKBACK_DAYS
from hh_search.storage.base import Housekeeper

logger = logging.getLogger(__name__)

# Имя файла отчёта начинается с даты: `2026-07-31-new.html`, `-new.csv`,
# `-new.md`, плюс отметка о доставке `-new.html.sent`. Всё, что под
# образец не подпадает, уборка не трогает — включая черновики `*.part`
# (у них дата тоже впереди, но суффикс чужой) и любые файлы человека.
_REPORT_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-[^/]*(?<!\.part)$")

# Сколько последних суток отчётов защищено в любом случае: окно довозки
# плюс сегодня. Один день сверх необходимого взят намеренно — цена
# лишнего файла на диске нулевая, цена удалённой отметки `.sent` —
# повторный документ в канале.
PROTECTED_DAYS = LOOKBACK_DAYS + 1

HORIZON_FILE = "last-cleanup"

_MB = 1024 * 1024


@dataclass(frozen=True)
class CleanupDays:
    """Сроки хранения. `reports=None` означает «файлы не трогать вовсе».

    Одним полем выражены и флаг `--reports`, и его срок: два поля
    разъехались бы при первой же правке CLI.
    """

    descriptions: int = 90
    runs: int = 365
    reports: int | None = None


@dataclass(frozen=True)
class CleanupPlan:
    """Что уборка сделает или сделала. Одна форма на оба случая.

    Одна, а не две: сухой прогон обязан печатать РОВНО то, что напечатал
    бы `--apply`, иначе он перестаёт быть предпросмотром.
    """

    descriptions: int
    description_bytes: int
    runs: int
    report_files: int
    report_bytes: int
    descriptions_cutoff: datetime

    def describe(self, applied: bool) -> str:
        verb = "убрано" if applied else "будет убрано"
        lines = [
            f"описаний: {self.descriptions} ({self.description_bytes / _MB:.1f} МБ) — {verb}",
            f"строк журнала прогонов: {self.runs} — {verb}",
            f"файлов отчётов: {self.report_files} ({self.report_bytes / _MB:.1f} МБ) — {verb}",
            f"граница хранения описаний: {self.descriptions_cutoff:%Y-%m-%d}",
        ]
        if applied:
            lines.append(
                "база ужата (VACUUM). `report --since` за границу описаний "
                "больше не покажет вакансий — предупреждение об этом печатает сам `report`"
            )
        return "\n".join(lines)


def plan(
    repo: Housekeeper, reports_dir: Path, now: datetime, days: CleanupDays
) -> CleanupPlan:
    """Посчитать, ничего не меняя."""
    victims = _victim_files(reports_dir, now, days)
    rows, size = repo.descriptions_before(now - timedelta(days=days.descriptions))
    return CleanupPlan(
        descriptions=rows,
        description_bytes=size,
        runs=repo.count_runs_before(now - timedelta(days=days.runs)),
        report_files=len(victims),
        report_bytes=sum(path.stat().st_size for path in victims),
        descriptions_cutoff=now - timedelta(days=days.descriptions),
    )


def execute(
    repo: Housekeeper, reports_dir: Path, state_dir: Path, now: datetime, days: CleanupDays
) -> CleanupPlan:
    """Убрать и вернуть то, что убрано.

    Порядок: сначала файлы, потом база, потом `VACUUM`, потом горизонт.
    Горизонт последним потому, что он — обещание «за этой датой описаний
    нет», и записывать его до того, как они действительно убраны, значило
    бы обещать за уборку, которая могла и не случиться.
    """
    victims = _victim_files(reports_dir, now, days)
    removed_bytes = 0
    removed = 0
    for path in victims:
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as error:
            logger.error("файл отчёта %s не удалён: %s", path, error)
            continue
        removed += 1
        removed_bytes += size
    cutoff = now - timedelta(days=days.descriptions)
    _, size_before = repo.descriptions_before(cutoff)
    descriptions = repo.forget_descriptions(cutoff)
    runs = repo.forget_runs(now - timedelta(days=days.runs))
    repo.vacuum()
    _write_horizon(state_dir, cutoff)
    return CleanupPlan(
        descriptions=descriptions,
        description_bytes=size_before,
        runs=runs,
        report_files=removed,
        report_bytes=removed_bytes,
        descriptions_cutoff=cutoff,
    )


def horizon(state_dir: Path) -> date | None:
    """Граница последней уборки описаний или `None`, если её не было.

    Нечитаемый файл — тоже `None`: предупреждение, роняющее команду
    `report`, хуже отсутствующего предупреждения.
    """
    try:
        return date.fromisoformat((state_dir / HORIZON_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_horizon(state_dir: Path, cutoff: datetime) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / HORIZON_FILE).write_text(f"{cutoff:%Y-%m-%d}\n", encoding="utf-8")


def _victim_files(reports_dir: Path, now: datetime, days: CleanupDays) -> list[Path]:
    """Файлы отчётов под удаление. Защищённое окно не отдаётся никогда."""
    if days.reports is None or not reports_dir.exists():
        return []
    cutoff = now.date() - timedelta(days=days.reports)
    protected_from = now.date() - timedelta(days=PROTECTED_DAYS)
    victims = []
    for path in sorted(reports_dir.iterdir()):
        if not path.is_file():
            continue
        day = _day_of(path.name)
        if day is None or day >= cutoff or day >= protected_from:
            continue
        victims.append(path)
    return victims


def _day_of(name: str) -> date | None:
    match = _REPORT_NAME_RE.match(name)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
```

- [ ] **Шаг 4: Прогнать тесты задачи**

Run: `uv run pytest tests/test_cleanup.py -v`
Expected: PASS.

- [ ] **Шаг 5: Проверить бюджет строк нового модуля**

Run:
```bash
uv run python -c "
import ast, pathlib
src = pathlib.Path('hh_search/pipeline/cleanup.py').read_text()
tree = ast.parse(src)
docs = set()
for node in ast.walk(tree):
    body = getattr(node, 'body', None)
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and body:
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docs.update(range(first.lineno, first.end_lineno + 1))
print(sum(1 for i, l in enumerate(src.splitlines(), 1)
          if l.strip() and not l.strip().startswith('#') and i not in docs))
"
```
Expected: меньше 150. Если больше — вынести `_victim_files`/`_day_of` в
`hh_search/pipeline/report_files.py`, а не выписывать исключение в §4.3.

- [ ] **Шаг 6: Дерево модулей §4.3**

Рядом с `reporting.py`:
```
    cleanup.py            ручная уборка: план, исполнение, горизонт хранения
```

- [ ] **Шаг 7: Ворота и коммит**

Run: `./gate`

```bash
git add hh_search/pipeline/cleanup.py tests/test_cleanup.py docs/superpowers/specs/
git commit -m "feat: план и исполнение уборки, защита окна довозки (R-1)"
```

---

## Task 5: R-1c — команда `cleanup` и предупреждение в `report --since`

**Files:**
- Modify: `hh_search/__main__.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (§4.3 строка про
  число команд, §13 пункт 5)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `plan`, `execute`, `horizon`, `CleanupDays` из `hh_search/pipeline/cleanup.py`.
- Produces: команду CLI `cleanup`; предупреждение в `report_command`.

- [ ] **Шаг 1: Написать падающие тесты в `tests/test_cli.py`**

```python
def test_cleanup_without_apply_changes_nothing(tmp_path: Path) -> None:
    """Без флага команда печатает план в БУДУЩЕМ времени и не трогает диск.

    Для необратимой команды, которую зовут раз в несколько месяцев,
    забыть добавить флаг безопаснее, чем забыть его убрать.
    """
    ...
    result = invoke(["cleanup"])
    assert result.exit_code == 0
    assert "будет убрано" in result.output
    assert db.read_bytes() == before


def test_cleanup_apply_clears_descriptions_and_says_so(tmp_path: Path) -> None:
    ...
    result = invoke(["cleanup", "--apply"])
    assert "убрано" in result.output
    assert "VACUUM" in result.output


def test_cleanup_does_not_delete_report_files_without_the_flag(tmp_path: Path) -> None:
    ...


def test_cleanup_refuses_while_a_run_holds_the_lock(tmp_path: Path) -> None:
    """Уборка пишет в ту же базу, что демон, — значит берёт тот же замок."""
    ...
    with single_run(lock_path):
        result = invoke(["cleanup", "--apply"])
    assert result.exit_code != 0
    assert "прогон" in result.output


def test_report_warns_when_the_window_reaches_past_the_cleanup_horizon(tmp_path: Path) -> None:
    """`report --since 120` после уборки на 90 днях обязан сказать вслух.

    Иначе потеря тихая: выборки отчёта фильтруют по `description IS NOT
    NULL`, и человек получил бы 90 дней, попросив 120, без единого слова.
    """
    ...
    invoke(["cleanup", "--apply"])
    result = invoke(["report", "--since", "120d"])
    assert "уборк" in result.output
```

Разложить по образцу соседних тестов файла: там уже есть помощник запуска CLI
через `typer.testing.CliRunner` и подготовка каталога данных.

- [ ] **Шаг 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_cli.py -k cleanup -v`
Expected: FAIL — `No such command 'cleanup'`.

- [ ] **Шаг 3: Добавить команду в `hh_search/__main__.py`**

Рядом с `Since`:

```python
Apply = Annotated[bool, typer.Option("--apply", help="Выполнить уборку, а не показать план")]
Reports = Annotated[bool, typer.Option("--reports", help="Удалять и файлы отчётов")]
DescriptionsDays = Annotated[int, typer.Option("--descriptions-days", help="Срок хранения описаний")]
RunsDays = Annotated[int, typer.Option("--runs-days", help="Срок хранения журнала прогонов")]
ReportsDays = Annotated[int, typer.Option("--reports-days", help="Срок хранения файлов отчётов")]
```

Команда:

```python
@app.command("cleanup")
def cleanup_command(
    ctx: typer.Context,
    apply: Apply = False,
    reports: Reports = False,
    descriptions_days: DescriptionsDays = 90,
    runs_days: RunsDays = 365,
    reports_days: ReportsDays = 90,
) -> None:
    """Убрать старые описания, журнал прогонов и (по флагу) файлы отчётов.

    Без `--apply` печатает план и не трогает ничего. Замок тот же, что у
    прогона: команда пишет в ту же базу, что демон.
    """
    config = _config(ctx)
    days = CleanupDays(
        descriptions=descriptions_days,
        runs=runs_days,
        reports=reports_days if reports else None,
    )
    now = datetime.now(UTC)
    try:
        with _storage_errors(config), single_run(_lock_path(config)), _open(config) as repo:
            if apply:
                result = execute(repo, config.app.paths.reports, _state_dir(config), now, days)
            else:
                result = plan(repo, config.app.paths.reports, now, days)
    except RunInProgress as error:
        _die(str(error), EXIT_FAILED)
    except StorageUnavailable as error:
        _die(str(error), EXIT_FAILED)
    typer.echo(result.describe(applied=apply))
```

и помощник рядом с `_lock_path`:

```python
def _state_dir(config: Config) -> Path:
    """Каталог рядом с базой: там же лежат маркер 403 и горизонт уборки."""
    return config.app.paths.state.parent
```

- [ ] **Шаг 4: Добавить предупреждение в `report_command`**

Сразу после вычисления `cutoff`:

```python
    horizon_day = horizon(_state_dir(config))
    if horizon_day is not None and cutoff.date() < horizon_day:
        # Тихая потеря иначе: выборки отчёта фильтруют по
        # `description IS NOT NULL`, и убранные вакансии просто не
        # придут — без единого слова о том, что окно шире хранимого.
        typer.echo(
            f"описания старше {horizon_day:%Y-%m-%d} убраны командой `cleanup`: "
            f"за эту границу отчёт вакансий не покажет, хотя запрошено с "
            f"{cutoff:%Y-%m-%d}",
            err=True,
        )
```

- [ ] **Шаг 5: Прогнать тесты задачи**

Run: `uv run pytest tests/test_cli.py -k "cleanup or horizon" -v`
Expected: PASS.

- [ ] **Шаг 6: Обновить README**

Раздел с командами — дописать:

````markdown
### Уборка старых данных

Ничто не удаляется само: уборка ручная, зовите её раз в несколько месяцев.

```sh
docker compose run --rm hh-search cleanup                    # что будет убрано
docker compose run --rm hh-search cleanup --apply            # описания и журнал
docker compose run --rm hh-search cleanup --reports --apply  # плюс файлы отчётов
```

Без `--apply` команда ничего не трогает. Сроки по умолчанию: описания 90 дней,
журнал прогонов 365, файлы отчётов 90 (`--descriptions-days`, `--runs-days`,
`--reports-days`).

Что важно знать до первого запуска:

- **описания убираются, строки вакансий — нет.** Строка остаётся дедупликацией:
  удали её, и вакансия будет найдена заново, скачана ещё раз и повторно прислана
  в Telegram;
- **`report --since` за границу хранения описаний вакансий не покажет.** Команда
  предупредит об этом сама, когда запрошенное окно уходит за границу;
- **файлы отчётов удаляются только с `--reports`** — это единственное необратимое
  из трёх действий;
- файлы последних суток не удаляются никогда, даже с `--reports-days 0`: на них
  стоит довозка документов Telegram.
````

- [ ] **Шаг 7: Обновить спеку**

1. §4.3, таблица бюджета, строка `hh_search/__main__.py`: «Шесть команд» → «Семь
   команд».
2. §13, пункт 5: заменить «Ретенция данных не реализована нигде (задача R-1)» на
   описание сделанного со ссылкой на спеку 2026-08-01 и названными сроками
   по умолчанию.

- [ ] **Шаг 8: Ворота и коммит**

Run: `./gate`

```bash
git add hh_search/__main__.py README.md tests/test_cli.py docs/superpowers/specs/
git commit -m "feat: команда cleanup и предупреждение report --since за горизонтом (R-1)"
```

---

## Задача 6: приёмка волны

- [ ] **Шаг 1: Мутационный харнесс против новых сторожей**

Для каждого нового сторожа: внести мутацию в код, который он охраняет, убедиться,
что тест краснеет, откатить. Обязательное окружение:

```sh
NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
find . -name __pycache__ -type d -exec rm -rf {} +
```

Минимальный список мутаций (по одной за раз):

| Мутация | Обязан покраснеть |
|---|---|
| в `_parse_with_retry` ловить `FetchFailed` вместо `DegenerateListing` | `test_a_listing_that_is_not_ours_is_never_retried` |
| убрать `digest.rescued += 1` | сводка в `DegenerateDigest.log_summary` |
| вернуть `_redeliver` в ветку `if not fresh` | `test_a_run_with_nothing_to_report_still_redelivers_a_stuck_document` |
| звать `maintain_sinks` ПОСЛЕ `_collect` и раннего возврата | тот же тест |
| звать `maintain_sinks` после `emit_to_sinks` | `test_the_stuck_document_arrives_before_todays` |
| дать `maintain_sinks` пробрасывать исключение | `test_a_failing_maintain_does_not_degrade_the_run` |
| убрать `status = 'reported'` из `forget_descriptions` | `test_a_cleaned_vacancy_is_never_fetched_again` |
| убрать `description IS NOT NULL` оттуда же | `test_forget_descriptions_is_idempotent` |
| `LENGTH(description)` вместо `LENGTH(CAST(... AS BLOB))` | `test_descriptions_before_counts_bytes_not_characters` |
| убрать `repo.vacuum()` из `execute` | `test_vacuum_shrinks_the_file_after_descriptions_are_cleared` |
| `PROTECTED_DAYS = 0` | `test_the_redelivery_window_survives_reports_days_zero` |
| `execute` пишет горизонт ДО уборки | нужен новый сторож, если ни один не покраснел |

- [ ] **Шаг 2: Живой прогон на рабочих данных**

```sh
uv run hh-search cleanup                 # сухой, посмотреть числа
uv run hh-search cleanup --apply
uv run hh-search report --since 7d
```

Ожидается: сухой прогон не меняет размер `data/state/hh.db`; `--apply` печатает
границу и число убранных описаний; `report --since 7d` работает как раньше.

- [ ] **Шаг 3: Контрактные сетевые тесты**

Run: `uv run pytest -m network`
Expected: зелёные. R-3 меняет поведение при разборе живого листинга — контрактный
случай обязан пройти.

- [ ] **Шаг 4: Обновить `HANDOFF.md`**

Состояние, ветки, ближайший шаг. Закрыть R-1, R-3, T-2; оставить R-2 и R-5 как
принятые риски со ссылкой на §4 спеки 2026-08-01. Правила туда не возвращать.

- [ ] **Шаг 5: Финальные ворота и слияние**

Run: `./gate`

```bash
git add docs/superpowers/HANDOFF.md
git commit -m "docs: волна R-1, R-3 и T-2 закрыта, HANDOFF обновлён"
```

---

## Самопроверка плана

**Покрытие спеки.** §1 R-3 → Task 1. §2 T-2 → Task 2. §3 R-1 → Tasks 3–5 (§3.1
сроки → Task 3 и 5; §3.2 форма команды → Task 5; §3.3 четыре решения → `VACUUM`
Task 3, безопасность обнуления Task 3, защита окна Task 4, горизонт Task 4 и 5;
§3.4 размещение → Tasks 3 и 4). §4 отклонённое — кода не требует. §5 сторожа 1–10
→ распределены по задачам, мутационная проверка в Задаче 6. §6 документы → шаги
в Tasks 1, 3, 4, 5 и Задаче 6.

**Расхождение со спекой, поправленное здесь:** граница строк журнала — по
`finished_at`, а не по `started_at` (Task 3, шаг 8 правит спеку).

**Согласованность имён.** `DegenerateListing`, `DegenerateDigest`, `store_page`,
`Sink.maintain`, `maintain_sinks`, `Housekeeper`, `Retention`,
`descriptions_before`, `forget_descriptions`, `count_runs_before`, `forget_runs`,
`vacuum`, `CleanupDays`, `CleanupPlan`, `plan`, `execute`, `horizon`,
`PROTECTED_DAYS`, `HORIZON_FILE` — употребляются одинаково во всех задачах.
