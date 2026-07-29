import gzip
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.errors import AccessForbidden
from hh_search.pipeline import RunStats, run_once
from hh_search.pipeline.discovery import prefilter
from hh_search.pipeline.stats import RunCounters
from hh_search.scoring.base import Scorer
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.base import (
    REJECT_CODE_ENRICH_FAILED,
    REJECT_CODE_PREFILTER,
    STATUS_NEW,
    STATUS_REJECTED,
    STATUS_REPORTED,
    Repository,
)
from hh_search.storage.repository import SqliteRepository
from hh_search.storage.run_log import ALLOWED_RUN_COUNTERS
from tests.test_config import APP_YAML, PROFILE_YAML, write_config

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)

# Один листинг, одна страница: `pages: 2` из образца конфига удвоило бы
# каждое число в ожиданиях, ничего не добавив. Пагинация проверяется
# отдельным тестом, где она и есть предмет.
ONE_PAGE = """
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
    pages: 1
"""

LISTING_URL = "https://hh.ru/vacancies/programmist"
PAGE_PATTERN = r"^https://hh\.ru/vacancy/\d+$"


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


def listing_html(*vacancies: tuple[str, str], slug: str = "programmist") -> str:
    """Страница листинга ровно того устройства, что живая: canonical + ItemList."""
    items = [
        {"@type": "ListItem", "url": f"https://hh.ru/vacancy/{vacancy_id}", "name": title}
        for vacancy_id, title in vacancies
    ]
    block = json.dumps({"@type": "ItemList", "itemListElement": items}, ensure_ascii=False)
    return (
        f'<html><head><link rel="canonical" href="https://hh.ru/vacancies/{slug}">'
        f'<script type="application/ld+json">{block}</script></head><body></body></html>'
    )


def page_html(description: str = "Опыт Yocto и Buildroot.") -> str:
    block = json.dumps(
        {
            "@type": "JobPosting",
            "description": f"<p>{description}</p>",
            "hiringOrganization": {"name": "ООО Ромашка"},
        },
        ensure_ascii=False,
    )
    return f'<html><script type="application/ld+json">{block}</script></html>'


TWO_VACANCIES = listing_html(
    ("111", "Senior Embedded Engineer"), ("222", "Junior Python Developer")
)
# Тот же листинг, но БЕЗ стоп-слова во втором заголовке: там, где предмет
# теста — очередь обогащения, «junior» отсеял бы половину выборки ещё
# префильтром, и проверка считала бы не то, что проверяет.
TWO_ENRICHABLE = listing_html(
    ("111", "Senior Embedded Engineer"), ("222", "Middle Python Developer")
)


class RecordingSink:
    """Приёмник, который помнит, что и сколько раз ему отдали."""

    def __init__(self, name: str = "recording", fail: bool = False) -> None:
        self.name = name
        self.batches: list[list[str]] = []
        # Сохраняется не только id: часть проверок читает поля, за которые
        # заплачено запросом к странице вакансии, а приёмник получает их
        # уже ПОСЛЕ обратного чтения из базы.
        self.items: list[ScoredVacancy] = []
        self._fail = fail

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        if self._fail:
            raise RuntimeError(f"приёмник {self.name} недоступен")
        self.batches.append([item.discovered.id for item in vacancies])
        self.items.extend(vacancies)
        # Приёмник записывает всё, что ему отдали: дедупликации файла дня
        # у него нет, и притворяться ею он не должен.
        return len(vacancies)

    @property
    def seen(self) -> list[str]:
        return [vacancy_id for batch in self.batches for vacancy_id in batch]


class BrokenScorer:
    """Скорер, падающий на первых `failures` вызовах.

    `ZeroDivisionError` — не выдумка: `saturation: 0` в profile.yaml даёт
    ровно её, причём уже ПОСЛЕ того, как страница скачана.
    """

    def __init__(self, profile: Config, failures: int = 1_000_000) -> None:
        self._real = KeywordScorer(profile.profile)
        self._left = failures
        self.calls = 0

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ZeroDivisionError("division by zero")
        return self._real.score(discovered, details)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return load_config(write_config(tmp_path, **{"queries.yaml": ONE_PAGE}))


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Путь к файловой базе: нужен там, где база портится сырым SQL."""
    return str(tmp_path / "hh.db")


def make_client(config: Config) -> PoliteClient:
    return PoliteClient(config.app.http, config.app.user_agent, sleep=lambda _: None)


def mock_source(
    listing: str = TWO_VACANCIES,
    page: str | httpx.Response | None = None,
    listing_headers: dict[str, str] | None = None,
) -> tuple[respx.Route, respx.Route, respx.Route]:
    """robots.txt, листинг и страницы вакансий — все три обязательны.

    robots ОБЯЗАТЕЛЕН в каждом тесте: незамоканный запрос даёт
    `AllMockedAssertionError`, а это подкласс `AssertionError`, который
    `_load_robots` не ловит (он ловит `httpx.HTTPError`). Такой прогон
    рвётся насквозь ещё до первого шага, и любой ассерт про конвейер
    проверяет пустоту. Правила берутся живые, из фикстуры: заодно каждый
    прогон конвейера перепроверяет, что выбранные URL источником разрешены.
    """
    robots = respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    listing_route = respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(200, text=listing, headers=listing_headers or {})
    )
    body = page if page is not None else page_html()
    page_response = body if isinstance(body, httpx.Response) else httpx.Response(200, text=body)
    page_route = respx.get(url__regex=PAGE_PATTERN).mock(return_value=page_response)
    return robots, listing_route, page_route


def mock_robots() -> respx.Route:
    """Живые правила hh.ru — там, где листинг мокается отдельным side_effect."""
    return respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )


def run(
    config: Config,
    repo: SqliteRepository,
    sinks: Sequence[Sink],
    scorer: Scorer | None = None,
    now: datetime = NOW,
) -> RunStats:
    with make_client(config) as client:
        return run_once(config, client, repo, scorer or KeywordScorer(config.profile), sinks, now)


def journal(db: str) -> list[tuple[object, ...]]:
    raw = sqlite3.connect(db)
    rows = raw.execute(
        "SELECT status, discovered, new_count, rejected, enriched, rescored, stuck, reported "
        "FROM run ORDER BY id"
    ).fetchall()
    raw.close()
    return [tuple(row) for row in rows]


def corrupt(db: str, sql: str, *params: object) -> None:
    raw = sqlite3.connect(db)
    raw.execute(sql, params)
    raw.commit()
    raw.close()


# --- прогон целиком, на живых фикстурах -----------------------------------


@respx.mock
def test_live_listing_run_ends_with_measured_counters(
    config: Config, repo: SqliteRepository
) -> None:
    """Живая страница `/vacancies/programmist` и живая страница вакансии.

    Числа зафиксированы по факту: 20 элементов в ItemList, два заголовка с
    «junior» отсеяны префильтром (в профиле образца одно стоп-слово), 18
    страниц скачано, 18 вакансий отправлено. Сверяется ВЕСЬ набор
    счётчиков, потому что именно они уезжают в журнал прогона и в
    healthcheck.
    """
    mock_source(listing=load("listing_programmist.html.gz"), page=load("vacancy_salary.html.gz"))
    sink = RecordingSink()
    stats = run(config, repo, [sink])
    assert (stats.status, stats.error) == ("ok", None)
    assert (stats.discovered, stats.new_count, stats.rejected) == (20, 20, 2)
    assert (stats.enriched, stats.rescored, stats.stuck, stats.reported) == (18, 0, 0, 18)
    assert len(sink.seen) == 18
    assert "135469420" not in sink.seen  # «Программист 1С (стажер/junior)»


@respx.mock
def test_report_carries_the_fields_bought_by_the_page_request(
    config: Config, repo: SqliteRepository
) -> None:
    """Компания, регион, зарплата и дата публикации доезжают до приёмника.

    Листинг их не отдаёт вовсе — за них заплачено запросом к странице
    вакансии, и они обязаны пройти весь путь: разбор → `save_enriched` →
    `unreported()` → приёмник. Тест читает их из того, что получил
    приёмник, то есть уже ПОСЛЕ обратного чтения из базы.
    """
    mock_source(
        listing=listing_html(("111", "Senior Embedded Engineer")),
        page=load("vacancy_salary.html.gz"),
    )
    sink = RecordingSink()
    run(config, repo, [sink])
    assert [vacancy.discovered.id for vacancy in sink.items] == ["111"]
    discovered = sink.items[0].discovered
    assert (discovered.company, discovered.area) == ("Альтео Софт", "Москва")
    assert (discovered.salary.amount_from, discovered.salary.amount_to) == (100000, 150000)
    assert discovered.published_at == datetime(2026, 7, 27, 16, 27, 20, 492000, tzinfo=UTC)


@respx.mock
def test_rejected_vacancy_is_never_fetched(config: Config, repo: SqliteRepository) -> None:
    """Скачиваются РОВНО выжившие — ни больше, ни меньше.

    Прежняя редакция проверяла только отсутствие одного URL и потому
    проходила даже тогда, когда обогащение не скачивало вообще ничего.
    """
    _, _, page_route = mock_source()
    sink = RecordingSink()
    run(config, repo, [sink])
    requested = sorted(call.request.url.path for call in page_route.calls)
    assert requested == ["/vacancy/111"]
    assert sink.seen == ["111"]


# --- страница качается один раз за жизнь вакансии --------------------------


@respx.mock
def test_second_run_costs_one_request_and_reports_nothing(
    config: Config, repo: SqliteRepository
) -> None:
    """Считаются ФАКТИЧЕСКИЕ запросы, а не результат.

    Второй прогон обязан стоить одного запроса к листингу и ни одного к
    страницам вакансий: описание уже записано, а `pending_enrichment`
    выбирает только `description IS NULL`. Проверка «во втором прогоне
    ничего не отправлено» этого не ловит — она зелена и когда конвейер
    качает всё заново.
    """
    _, listing_route, page_route = mock_source()
    scorer = KeywordScorer(config.profile)
    first = run(config, repo, [RecordingSink()], scorer)
    assert (listing_route.call_count, page_route.call_count) == (1, 1)

    sink = RecordingSink()
    second = run(config, repo, [sink], scorer)
    assert (listing_route.call_count, page_route.call_count) == (2, 1)
    assert (second.new_count, second.reported, second.status) == (0, 0, "ok")
    assert sink.batches == []
    assert first.reported == 1


@respx.mock
def test_pages_of_one_listing_are_requested_one_by_one(
    tmp_path: Path, repo: SqliteRepository
) -> None:
    """`pages: 2` — это два запроса, второй с `?page=1` (нумерация с нуля).

    Без этого теста конвейер, читающий только первую страницу, выглядел бы
    полностью работоспособным: вакансии есть, отчёт есть, статус `ok`.
    """
    config = load_config(write_config(tmp_path))
    _, listing_route, _ = mock_source(listing=TWO_VACANCIES)
    run(config, repo, [RecordingSink()])
    assert [str(call.request.url) for call in listing_route.calls] == [
        LISTING_URL,
        f"{LISTING_URL}?page=1",
    ]


# --- C3: валидатор условного запроса пишется ПОСЛЕ записи вакансий ---------


@respx.mock
def test_unparsable_listing_leaves_no_cache_validator(
    config: Config, repo: SqliteRepository
) -> None:
    """Валидатор снимка, который не был прочитан, — вечный 304.

    Проверяется и то, что новый не записан, и то, что прежний СБРОШЕН:
    иначе одна обрезанная выдача ослепляет сервис навсегда, причём при
    зелёном healthcheck и чистом `docker logs`.
    """
    repo.save_cache_headers(LISTING_URL, '"stale-v0"', None)
    mock_source(
        listing='<html><head><link rel="canonical" href="/vacancies/programmist">'
        "</head><body>без ItemList</body></html>"
    )
    stats = run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {}
    assert stats.status == "failed"


@respx.mock
def test_truncated_listing_does_not_blind_the_next_run(
    config: Config, repo: SqliteRepository
) -> None:
    """Сквозная форма того же дефекта: авария не нужна, хватит одной обрезки.

    Мок отвечает 304 на условный запрос — ровно как hh.ru. Если первый
    прогон сохранит валидатор до разбора, второй получит 304, вакансий не
    увидит никогда и отчитается `ok`.
    """
    broken = '<html><head><link rel="canonical" href="/vacancies/programmist"></head></html>'
    state = {"listing": broken}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match"):
            return httpx.Response(304)
        return httpx.Response(200, text=state["listing"], headers={"ETag": '"v1"'})

    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(side_effect=answer)
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))

    first = run(config, repo, [RecordingSink()])
    assert (first.status, first.discovered) == ("failed", 0)

    state["listing"] = TWO_VACANCIES
    sink = RecordingSink()
    second = run(config, repo, [sink])
    assert (second.status, second.discovered, second.reported) == ("ok", 2, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_validator_is_not_stored_when_writing_vacancies_fails(
    config: Config, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сторож самого ПОРЯДКА, а не только сброса кэша.

    Сброс `reset_cache` спасает лишь тот отказ, который мы предвидели —
    отказ разбора. Между разбором и записью в базу может случиться что
    угодно: заблокированная база, кончившееся место, убитый контейнер. Если
    валидатор к этому моменту уже записан, следующий прогон получит 304 и
    не увидит эти вакансии НИКОГДА.
    """
    mock_source(listing_headers={"ETag": '"v1"'})

    def locked(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "add_discovered", locked)
    with pytest.raises(sqlite3.OperationalError):
        run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {}


@respx.mock
def test_validator_is_saved_after_a_good_page(config: Config, repo: SqliteRepository) -> None:
    """Обратная сторона: на успешном разборе валидатор обязан сохраниться,
    иначе условные запросы не работают вовсе и каждый прогон тянет тело."""
    mock_source(listing_headers={"ETag": '"v1"'})
    run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {"If-None-Match": '"v1"'}


# --- R-I3: агрегатный сторож тишины ---------------------------------------


@respx.mock
def test_run_where_no_listing_yielded_anything_is_a_failure(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Пустая страница законна, пустой ПРОГОН — нет.

    `itemListElement: []` разбирается без ошибки (честно пустая выдача), и
    без агрегатного сторожа прогон отчитался бы `ok` при нулевой работе —
    класс «месяцы молчания при зелёном healthcheck».
    """
    mock_source(listing=listing_html())
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered) == ("failed", 0)
    assert stats.exit_code() == 1
    assert "ни одна не дала ни одной вакансии" in caplog.text


@respx.mock
def test_one_empty_page_among_several_is_not_a_failure(
    tmp_path: Path, repo: SqliteRepository
) -> None:
    """Сторож обязан быть АГРЕГАТНЫМ: пустая вторая страница — норма для
    листинга, который короче двух страниц."""
    config = load_config(write_config(tmp_path))

    def answer(request: httpx.Request) -> httpx.Response:
        body = listing_html() if request.url.query else TWO_VACANCIES
        return httpx.Response(200, text=body)

    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(side_effect=answer)
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered, stats.reported) == ("ok", 2, 1)


@respx.mock
def test_run_without_a_single_fetched_listing_is_a_failure(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Полная потеря сети обязана быть `failed`, а не `partial`.

    Наблюдение Task 12, воспроизведённое в офлайн-контейнере: каждый
    запрос падает, каждый отказ отдельно — законный `partial`, а `partial`
    считается успехом для `last_successful_run()`. Итог — healthcheck
    возвращает 0 вечно при сервисе, который не делает ничего: ровно тот
    класс отказа, ради которого healthcheck и заведён. Сторож тишины висел
    на страницах, ОТДАННЫХ источником, и при нуле отданных молчал.
    """
    respx.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("Network is unreachable"))
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered) == ("failed", 0)
    assert stats.exit_code() == 1
    assert "ни одна из 1 запрошенных страниц листингов не получена" in caplog.text
    # Вторая половина того же факта: журнал прогонов не считает этот
    # прогон успешным, то есть healthcheck его не увидит.
    assert repo.last_successful_run() is None


@respx.mock
def test_run_where_every_listing_is_unchanged_stays_successful(
    config: Config, repo: SqliteRepository
) -> None:
    """Обратная сторона: `304` — это ОТВЕТ источника, а не тишина.

    Прогон, в котором ничего не изменилось с прошлого раза, — штатный и
    самый частый исход, и записывать его в `failed` значило бы держать
    healthcheck красным между публикациями вакансий.
    """
    repo.save_cache_headers(LISTING_URL, '"v1"', None)
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(return_value=httpx.Response(304))
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered) == ("ok", 0)
    assert repo.last_successful_run() is not None


# --- C4: авария источника не жжёт попытки ---------------------------------


@respx.mock
def test_source_outage_does_not_burn_enrich_attempts(
    config: Config, repo: SqliteRepository
) -> None:
    """Три прогона при лежащем hh.ru — очередь обязана остаться целой.

    `max_attempts = 3`, то есть прежняя редакция плана к третьему прогону
    отправляла всю очередь в `rejected`/`enrich_failed` терминально, и
    вернуть её было нечем: `add_discovered` даёт False, а
    `pending_enrichment` требует `description IS NULL`. 12 часов
    недоступности источника стоили всего бэклога.
    """
    mock_source(page=httpx.Response(503))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        stats = run(config, repo, [RecordingSink()], scorer)
        assert (stats.enriched, stats.status) == (0, "partial")
    assert [vacancy.id for vacancy in repo.pending_enrichment(3)] == ["111"]

    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    sink = RecordingSink()
    recovered = run(config, repo, [sink], scorer)
    assert (recovered.enriched, recovered.reported) == (1, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_broken_page_burns_attempts_and_ends_in_enrich_failed(config: Config, db_path: str) -> None:
    """Обратная сторона того же разделения: 404 — состояние ВАКАНСИИ.

    Счётчик обязан работать, иначе несуществующая вакансия перепрашивается
    вечно. Терминальный статус ставит тем же UPDATE `bump_enrich_attempt`.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source(page=httpx.Response(404))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        run(config, disk, [RecordingSink()], scorer)
    assert repo_status(db_path, "111") == ("rejected", "enrich_failed", 3)
    assert disk.pending_enrichment(3) == []
    disk.close()


def repo_status(db: str, vacancy_id: str) -> tuple[object, object, object]:
    raw = sqlite3.connect(db)
    row = raw.execute(
        "SELECT status, reject_reason, enrich_attempts FROM vacancy WHERE id = ?", (vacancy_id,)
    ).fetchone()
    raw.close()
    return tuple(row)


# --- I3: отказ оценки не выбрасывает скачанную страницу -------------------


@respx.mock
def test_scoring_failure_keeps_the_page_and_is_loud(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Скоринг — локальное вычисление; страница за ним уже стоила запроса.

    Проверяется всё, что здесь дорого: прогон не упал, страница скачана
    РОВНО один раз за оба прогона, вакансия ждёт в `pending_scoring`,
    остаток очереди назван в логе поимённо, статус понижен, — и второй
    прогон досчитывает оценку, НЕ обращаясь к hh.ru.

    Прежняя редакция вызывала `scorer.score(...)` прямо аргументом записи,
    поэтому `ZeroDivisionError` (достижимая опечаткой `saturation: 0`)
    роняла прогон целиком и выбрасывала уже скачанную страницу.
    """
    _, _, page_route = mock_source()
    with caplog.at_level(logging.ERROR):
        first = run(config, repo, [RecordingSink()], BrokenScorer(config))
    assert (first.status, first.enriched, first.reported, first.stuck) == ("partial", 0, 0, 1)
    assert page_route.call_count == 1
    assert [vacancy.id for vacancy, _ in repo.pending_scoring()] == ["111"]
    assert "111" in caplog.text

    sink = RecordingSink()
    second = run(config, repo, [sink], KeywordScorer(config.profile))
    assert page_route.call_count == 1
    assert (second.rescored, second.stuck, second.reported) == (1, 0, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_stuck_scoring_queue_is_reported_once_and_without_a_false_cause(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """I1: один факт — одно сообщение, и причина в нём не выдумана.

    Сторож жил в `unreported()`, а конвейер зовёт её дважды за прогон,
    поэтому одна застрявшая вакансия давала ТРИ `ERROR` (два из
    хранилища, один из конвейера), и два из трёх утверждали: «конвейер не
    вызвал pending_scoring() перед unreported()». Утверждение ложно
    именно там, где сторож и срабатывает: за этот же прогон конвейер
    зовёт `pending_scoring()` трижды, а очередь не сходится потому, что
    отказал скорер — о чём строкой выше кричит сам `rescore`.

    Проверяется поэтому не только число сообщений, но и отсутствие
    выдуманной причины: диагностика этого сервиса держится на том, что
    сообщение называет причину верно.
    """
    mock_source()
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()], BrokenScorer(config))
    assert stats.stuck == 1
    about_stuck = [
        record.getMessage()
        for record in caplog.records
        if "не попадут в отчёт" in record.getMessage()
    ]
    assert len(about_stuck) == 1
    assert "111" in about_stuck[0]
    assert "pending_scoring()" not in caplog.text
    assert "конвейер не вызвал" not in caplog.text


@respx.mock
def test_score_is_recomputed_and_sent_within_one_run(config: Config, db_path: str) -> None:
    """Порча оценки лечится за ОДИН прогон, а не за два.

    Карантин срабатывает внутри `unreported()`: нечитаемая оценка
    обнуляется в момент чтения, и вакансия уходит в `pending_scoring`
    уже после того, как отправлять было бы поздно. Поэтому пересчёт стоит
    между двумя чтениями — один вызов задерживал бы вылеченную вакансию
    до следующего прогона.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source()
    scorer = KeywordScorer(config.profile)
    run(config, disk, [RecordingSink()], scorer)
    corrupt(
        db_path,
        "UPDATE vacancy SET status = 'new', reported_at = NULL, score_detail = ? WHERE id = '111'",
        "{не json",
    )

    sink = RecordingSink()
    stats = run(config, disk, [sink], scorer)
    assert (stats.rescored, stats.stuck, stats.reported) == (1, 0, 1)
    assert sink.seen == ["111"]
    disk.close()


# --- I1: частичный отказ приёмника -----------------------------------------


@respx.mock
def test_a_run_that_delivered_nothing_is_failed_not_partial(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """C2: `partial` считается успехом для healthcheck — значит «ничего не
    доставлено» им быть не может.

    Приёмники строятся из конфига, а конфиг указывает на том. Том,
    смонтированный не туда, роняет ВСЕ приёмники: вакансии остаются в
    очереди, отчётов нет, и так прогон за прогоном. При `partial`
    `last_successful_run()` считает такой прогон успешным, и
    `healthcheck` возвращает 0 вечно — тот самый класс «процесс жив,
    работа не делается», ради которого healthcheck и заводился.
    Один живой приёмник из двух остаётся `partial`: часть работы дошла.
    """
    mock_source()
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink("csv", fail=True)])
    assert (stats.status, stats.reported) == ("failed", 0)
    assert stats.exit_code() == 1
    assert [vacancy.discovered.id for vacancy in repo.unreported()] == ["111"]


@respx.mock
def test_partial_sink_failure_keeps_everything_unreported(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Ни одна вакансия не помечается отправленной, и об этом громко.

    Повтор при этом неустраним: живой приёмник получит те же вакансии
    следующим прогоном. Тест фиксирует и это — иначе «защита от потери»
    выглядела бы бесплатной.
    """
    mock_source()
    good = RecordingSink("csv")
    bad = RecordingSink("markdown", fail=True)
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [good, bad])
    assert (stats.status, stats.reported) == ("partial", 0)
    assert "markdown" in caplog.text and "повторно" in caplog.text
    assert good.seen == ["111"]
    assert [vacancy.discovered.id for vacancy in repo.unreported()] == ["111"]

    both = RecordingSink("csv")
    second = run(config, repo, [both])
    assert (second.status, second.reported) == ("ok", 1)
    assert both.seen == ["111"]


@respx.mock
def test_run_without_sinks_marks_nothing_reported(config: Config, repo: SqliteRepository) -> None:
    """Пустой список приёмников — не повод пометить вакансии отправленными.

    Через конфиг недостижимо (`sinks` требует непустого списка), но именно
    такие пути в этом проекте обязаны кричать, а не молчать.
    """
    mock_source()
    stats = run(config, repo, [])
    assert (stats.status, stats.reported) == ("failed", 0)
    assert [vacancy.discovered.id for vacancy in repo.unreported()] == ["111"]


# --- журнал прогона --------------------------------------------------------


@respx.mock
def test_forbidden_stops_the_run_and_closes_the_journal(tmp_path: Path, db_path: str) -> None:
    """Устойчивый 403 останавливает прогон (спека §9), но строку журнала закрывает.

    Незакрытая строка `running` — это не косметика: healthcheck смотрит в
    журнал, и висящие строки копятся вечно.

    Листингов здесь два, а не один: остановку вызывает второй 403 ПОДРЯД —
    ровно то различие, которое спека называет словом «устойчивый» (см.
    `pipeline/forbidden.py`).
    """
    root = tmp_path / "two"
    root.mkdir(parents=True, exist_ok=True)
    two_pages = load_config(
        write_config(root, **{"queries.yaml": ONE_PAGE.replace("pages: 1", "pages: 2")})
    )
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(AccessForbidden):
        run(two_pages, disk, [RecordingSink()])
    assert disk.last_successful_run() is None
    assert journal(db_path) == [("failed", 0, 0, 0, 0, 0, 0, 0)]
    disk.close()


@respx.mock
def test_a_single_forbidden_page_does_not_throw_away_the_rest_of_the_run(
    config: Config, db_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Одиночный 403 на ОДНОЙ странице из двадцати — не закрытый источник.

    `PoliteClient` бросает `AccessForbidden` на первом же 403, и он летел
    из конвейера наружу мимо всех обработчиков: прогон обрывался, хотя
    остальные страницы отдавались нормально. `scheduler.py` это различие
    знает уровнем выше (`MAX_FORBIDDEN_IN_A_ROW`), в конвейере его не
    было вовсе — а два случайных 403 в двух прогонах подряд
    останавливали ещё и демон.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    pages = {str(100 + i): httpx.Response(200, text=page_html()) for i in range(4)}
    pages["102"] = httpx.Response(403)
    mock_source(listing=listing_html(*((key, "Инженер") for key in sorted(pages))))

    def answer(request: httpx.Request) -> httpx.Response:
        return pages[request.url.path.rsplit("/", 1)[-1]]

    respx.get(url__regex=PAGE_PATTERN).mock(side_effect=answer)
    sink = RecordingSink()
    with caplog.at_level(logging.WARNING):
        stats = run(config, disk, [sink], now=NOW)

    assert (stats.status, stats.enriched) == ("partial", 3)
    assert sorted(sink.seen) == ["100", "101", "103"]
    assert "подряд он пока один" in caplog.text
    # Попытка не сожжена: 403 — состояние источника, а не вакансии.
    assert repo_status(db_path, "102") == ("new", None, 0)
    disk.close()


@respx.mock
def test_counters_survive_a_crash_in_the_middle(
    config: Config, db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Счётчики накапливаются по ходу, а не присваиваются в конце.

    Падение на третьей вакансии из четырёх обязано оставить в журнале
    двойку: с присваиванием после возврата функции там был бы ноль, и
    журнал врал бы о том, сколько страниц уже скачано.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source(
        listing=listing_html(
            ("111", "Инженер"), ("222", "Разработчик"), ("333", "Программист"), ("444", "Тимлид")
        )
    )
    real = disk.save_enriched
    calls = {"n": 0}

    def flaky(vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise TypeError("база сломалась посреди прогона")
        real(vacancy_id, details, score)

    monkeypatch.setattr(disk, "save_enriched", flaky)
    with pytest.raises(TypeError):
        run(config, disk, [RecordingSink()])
    assert journal(db_path) == [("failed", 4, 4, 0, 2, 0, 0, 0)]
    disk.close()


def test_counter_names_match_the_run_table_whitelist() -> None:
    """Имя счётчика — не строка, которую можно опечатать.

    `finish_run` отбрасывает неизвестные имена МОЛЧА, поэтому опечатка в
    `RunCounters` стоила бы потерянного счётчика без единого признака.
    """
    assert set(RunCounters.__annotations__) <= ALLOWED_RUN_COUNTERS


def test_naive_moment_is_rejected(config: Config, repo: SqliteRepository) -> None:
    """Имя файла отчёта берётся из `now`, а `reported_at` пишется в UTC.

    Наивная дата при ночном прогоне разводит их на сутки, и найти отчёт
    по дате из базы становится невозможно.
    """
    with make_client(config) as client, pytest.raises(ValueError, match="aware UTC"):
        run_once(
            config, client, repo, KeywordScorer(config.profile), [], datetime(2026, 7, 28, 10, 0)
        )


# --- сторожа дрейфа источника ---------------------------------------------


@respx.mock
def test_salary_drift_guard_is_wired_into_enrichment(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """`SalaryBlockStats` обязан получать каждую страницу прогона.

    Не передать его в `parse_vacancy_page` — значит выключить сторож
    переименованного атрибута `data-qa="vacancy-salary"`, оставив его в
    коде. Проверяется на страницах без блока зарплаты.
    """
    mock_source()
    with caplog.at_level(logging.WARNING):
        run(config, repo, [RecordingSink()])
    assert 'data-qa="vacancy-salary"' in caplog.text


@respx.mock
def test_vacancy_page_drift_makes_the_run_failed(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Канарейка на смену вёрстки (спека §9): страница без JSON-LD.

    Прежняя редакция этого теста фиксировала `partial`, и это было
    закреплением дыры, а не поведения. `partial` считается успехом для
    `last_successful_run()`, то есть для healthcheck: сервис, который
    перестал понимать страницу вакансии, оставался зелёным. Замер на
    FIX_BASE (живой листинг из 20 вакансий, страница без JSON-LD): три
    прогона `partial`, четвёртый `ok` — вся очередь ушла в терминальный
    `enrich_failed`, отчётов не было ни одного, `healthcheck` отвечал
    кодом 0 «ok, последний успешный прогон».

    Ослаблением это не является: та же тишина уровнем ВЫШЕ уже решена
    именно так (`discovery._check_not_silent` — «источник отдал страницы,
    вакансий не найдено ни одной» → `failed`). Здесь тот же класс отказа
    и тот же ответ; расходились они только потому, что до страницы
    вакансии сторож не доехал.

    Причина отказа — сама страница, поэтому в логе обязана быть вёрстка,
    а не недоступность источника: лечит их разный человек.
    """
    mock_source(page="<html>без всякого JSON-LD</html>")
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.exit_code()) == ("failed", 1)
    assert "не разобрана НИ ОДНА" in caplog.text
    assert "сменил вёрстку" in caplog.text
    # Вторая половина того же факта: журнал прогонов не считает такой
    # прогон успешным, то есть healthcheck его не увидит.
    assert repo.last_successful_run() is None


@respx.mock
def test_four_runs_of_page_drift_never_look_successful(
    config: Config, db_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Сценарий целиком, тот самый, что был воспроизведён на FIX_BASE.

    Ключевое здесь — не статус одного прогона, а то, что ни один из
    прогонов, в которых очередь была и не обогатилась, не считается
    успешным. Четвёртый прогон разбирать уже нечего (вся очередь
    терминальна), и он снова `ok` — это честно: работы в нём нет, а
    красный индикатор к этому моменту горит уже три прогона подряд.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source(
        listing=load("listing_programmist.html.gz"), page="<html>без всякого JSON-LD</html>"
    )
    scorer = KeywordScorer(config.profile)
    with caplog.at_level(logging.ERROR):
        statuses = [run(config, disk, [RecordingSink()], scorer).status for _ in range(4)]
    assert statuses == ["failed", "failed", "failed", "ok"]
    assert [row[0] for row in journal(db_path)] == ["failed", "failed", "failed", "ok"]
    disk.close()


@respx.mock
def test_unreachable_pages_are_not_layout_drift(config: Config, repo: SqliteRepository) -> None:
    """Обратная сторона: источник, который страницу НЕ отдал, — не дрейф.

    Сторож считает страницы, отданные с кодом 200, а не запрошенные.
    Иначе авария источника (спека §9 — `WARNING` и `partial`, попытки не
    жжём) красила бы индикатор в красный, а решение C4 «отказ источника
    не жжёт очередь» превратилось бы в «отказ источника роняет прогон».
    """
    mock_source(page=httpx.Response(503))
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.enriched) == ("partial", 0)


@respx.mock
def test_pages_answering_404_are_not_layout_drift(config: Config, repo: SqliteRepository) -> None:
    """404 — состояние ВАКАНСИИ (снята), и очередь при нём честно тает.

    Прогон делает работу: тратит попытку и в конце концов закрывает
    вакансию. Это `partial`, а не `failed`, — иначе снятая вакансия
    красила бы индикатор.
    """
    mock_source(page=httpx.Response(404))
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.enriched) == ("partial", 0)


@respx.mock
def test_a_single_broken_page_does_not_fail_the_run(config: Config, repo: SqliteRepository) -> None:
    """Разобралась хотя бы одна — значит формат жив, а беда частная."""

    def answer(request: httpx.Request) -> httpx.Response:
        broken = request.url.path.endswith("/111")
        return httpx.Response(200, text="<html>пусто</html>" if broken else page_html())

    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(200, text=TWO_ENRICHABLE)
    )
    respx.get(url__regex=PAGE_PATTERN).mock(side_effect=answer)
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.enriched) == ("partial", 1)


# --- I4: однотипные отказы — одной строкой ---------------------------------


@respx.mock
def test_source_outage_does_not_flood_the_log(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Стена почти одинаковых WARNING — это потеря наблюдаемости.

    Замер на FIX_BASE (накопленный бэклог из 18 вакансий, источник лежит):
    20 строк `WARNING` и 3839 байт, отличающихся только URL, с полным
    текстом про robots.txt в каждой. Причина у всех одна, и названа она
    обязана быть один раз — иначе единственная строка, объясняющая, что
    случилось, тонет среди своих копий.
    """
    # Первый прогон копит очередь: страницы недоступны, попытки целы.
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(200, text=TWO_ENRICHABLE)
    )
    respx.get(url__regex=PAGE_PATTERN).mock(
        side_effect=httpx.ConnectError("Network is unreachable")
    )
    run(config, repo, [RecordingSink()])
    assert len(repo.pending_enrichment(3)) == 2

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hh_search.pipeline.failures"):
        run(config, repo, [RecordingSink()])
    lines = [record for record in caplog.records if record.name == "hh_search.pipeline.failures"]
    # Одна строка на страницы вакансий (их две, причина одна) — не две.
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert "2, причина у всех одна" in message
    assert message.count("Network is unreachable") == 1


# --- отказ префильтра перестаёт быть вечным -------------------------------
#
# Один список `negative` обслуживал два механизма с несопоставимой ценой
# ошибки: в скоринге совпадение стоит штраф и вакансия остаётся в отчёте,
# а в префильтре то же слово означало `status='rejected'` НАВСЕГДА. Слова
# образцового профиля писались как штрафные признаки, и в отсеве убивали
# целевые вакансии: «Backend-разработчик курьерской доставки» гибла от
# слова «курьер».

COURIER_TITLE = "Backend-разработчик курьерской доставки"
COURIER_LISTING = listing_html(("111", COURIER_TITLE))


def profile_with(negative: str) -> str:
    """Профиль образца, у которого различается только список стоп-слов."""
    return PROFILE_YAML.replace("negative: [junior]", f"negative: [{negative}]")


def config_with(root: Path, negative: str) -> Config:
    root.mkdir(parents=True, exist_ok=True)
    return load_config(
        write_config(root, **{"queries.yaml": ONE_PAGE, "profile.yaml": profile_with(negative)})
    )


def vacancy_table(db: str) -> list[tuple[object, ...]]:
    """Снимок таблицы вакансий целиком — сырым SQL, мимо любых выборок."""
    raw = sqlite3.connect(db)
    rows = raw.execute("SELECT * FROM vacancy ORDER BY id").fetchall()
    raw.close()
    return [tuple(row) for row in rows]


@respx.mock
def test_stop_word_removed_from_config_takes_the_vacancy_back_without_extra_requests(
    tmp_path: Path,
) -> None:
    """Сценарий целиком: опечатка в стоп-словах больше не стоит вакансии.

    Прогон 1 отбраковывает «Backend-разработчик курьерской доставки» по
    слову «курьер» — страница не скачивается вовсе, это и есть смысл
    префильтра. Пользователь убирает слово; прогон 2 обязан вернуть
    вакансию в очередь и довести до отчёта в ТОТ ЖЕ прогон.

    Считаются фактические HTTP-запросы, а не результат: на саму
    переоценку не тратится ни одного. Единственный новый запрос — та
    самая страница вакансии, за которой префильтр не пустил; он и есть
    работа, ради которой возврат делается.
    """
    db = str(tmp_path / "hh.db")
    repository = SqliteRepository(db)
    repository.init_schema()
    _, listing_route, page_route = mock_source(listing=COURIER_LISTING)

    with_word = config_with(tmp_path / "before", "курьер")
    first = run(with_word, repository, [RecordingSink()])
    assert (first.rejected, first.requeued, first.reported) == (1, 0, 0)
    assert page_route.call_count == 0, "отбракованная вакансия не имеет права стоить запроса"
    assert repo_status(db, "111")[0] == "rejected"

    without_word = config_with(tmp_path / "after", "junior")
    sink = RecordingSink()
    second = run(without_word, repository, [sink], KeywordScorer(without_word.profile))

    assert second.requeued == 1
    assert (second.rejected, second.enriched, second.reported) == (0, 1, 1)
    assert sink.seen == ["111"]
    assert repo_status(db, "111")[0] == "reported"
    # переоценка бесплатна: два листинга за два прогона и РОВНО одна
    # страница вакансии — та, которую префильтр раньше не пустил
    assert (listing_route.call_count, page_route.call_count) == (2, 1)
    repository.close()


@respx.mock
def test_prefilter_step_never_touches_the_network(tmp_path: Path) -> None:
    """Шаг 3 целиком, вместе с переоценкой, — ноль HTTP-вызовов.

    Проверка отдельная от сквозного сценария, потому что там запрос
    страницы вакансии законен и прячет собой любой лишний. Здесь их
    просто не может быть ни одного: заголовок лежит в базе с discovery.
    """
    db = str(tmp_path / "hh.db")
    repository = SqliteRepository(db)
    repository.init_schema()
    with_word = config_with(tmp_path / "before", "курьер")
    repository.add_discovered(
        DiscoveredVacancy(
            id="111",
            url="https://hh.ru/vacancy/111",
            title=COURIER_TITLE,
            found_by_query="programmist",
        ),
        "embedded",
        9,
    )
    prefilter(with_word, repository, RunStats())
    assert repo_status(db, "111")[0] == "rejected"

    without_word = config_with(tmp_path / "after", "junior")
    stats = RunStats()
    prefilter(without_word, repository, stats)

    assert stats.requeued == 1
    assert [v.id for v in repository.pending_enrichment(3)] == ["111"]
    assert respx.calls.call_count == 0, "переоценка отказа не имеет права ходить в сеть"
    repository.close()


@respx.mock
def test_unchanged_config_returns_nothing_and_does_not_touch_the_database(
    tmp_path: Path,
) -> None:
    """Дешевизна повторного прогона: ни возврата, ни единой записи.

    Сверяется вся таблица вакансий целиком, а не только статусы: возврат,
    гоняющий одни и те же строки прогон за прогоном, переписывал бы
    `enrich_attempts` и `reject_reason` незаметно для статусной проверки.
    """
    db = str(tmp_path / "hh.db")
    repository = SqliteRepository(db)
    repository.init_schema()
    mock_source(listing=COURIER_LISTING)
    config = config_with(tmp_path / "cfg", "курьер")
    scorer = KeywordScorer(config.profile)
    run(config, repository, [RecordingSink()], scorer)
    before = vacancy_table(db)

    second = run(config, repository, [RecordingSink()], scorer)

    assert (second.requeued, second.rejected, second.reported) == (0, 0, 0)
    assert vacancy_table(db) == before
    repository.close()


@respx.mock
def test_enrich_failure_is_not_taken_back_by_the_prefilter(config: Config, db_path: str) -> None:
    """`enrich_failed` переоценке заголовком не подлежит.

    Смысл у него другой: страница не разбирается (404, нет `JobPosting`),
    и заголовок про это не знает ничего. Возврат по коду, а не по тексту
    причины, — единственное, что удерживает эти два отказа врозь: текст
    префильтра меняется, и разъехавшийся префикс вернул бы в очередь
    несуществующие страницы, которые перепрашивались бы вечно.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    _, _, page_route = mock_source(page=httpx.Response(404))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        run(config, disk, [RecordingSink()], scorer)
    assert repo_status(db_path, "111") == ("rejected", "enrich_failed", 3)
    spent = page_route.call_count

    stats = run(config, disk, [RecordingSink()], scorer)

    assert stats.requeued == 0
    assert repo_status(db_path, "111") == ("rejected", "enrich_failed", 3)
    assert page_route.call_count == spent, "несуществующая страница не перепрашивается"
    assert "111" not in {key for key, _ in disk.rejected_by_prefilter()}
    disk.close()


@respx.mock
def test_requeued_count_reaches_the_run_journal(tmp_path: Path) -> None:
    """Возврат бэклога — метрика прогона, а не тихое событие.

    Без счётчика правка списка стоп-слов, достающая десятки вакансий,
    выглядела бы в журнале ровно как прогон, не сделавший ничего.
    """
    db = str(tmp_path / "hh.db")
    repository = SqliteRepository(db)
    repository.init_schema()
    mock_source(listing=COURIER_LISTING)
    with_word = config_with(tmp_path / "before", "курьер")
    run(with_word, repository, [RecordingSink()], KeywordScorer(with_word.profile))
    without_word = config_with(tmp_path / "after", "junior")
    run(without_word, repository, [RecordingSink()], KeywordScorer(without_word.profile))
    repository.close()

    raw = sqlite3.connect(db)
    requeued = [row[0] for row in raw.execute("SELECT requeued FROM run ORDER BY id")]
    raw.close()
    assert requeued == [0, 1]


# --- I1: снижение лимита попыток выводит вакансии из ВСЕХ очередей ---------


def config_with_attempts(root: Path, max_attempts: int) -> Config:
    root.mkdir(parents=True, exist_ok=True)
    app_yaml = APP_YAML.replace("max_attempts: 3", f"max_attempts: {max_attempts}")
    return load_config(write_config(root, **{"queries.yaml": ONE_PAGE, "app.yaml": app_yaml}))


@respx.mock
def test_lowered_attempt_limit_is_counted_and_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Правка `enrich.max_attempts` вниз обязана быть видимой.

    `pending_enrichment` отбирает по `enrich_attempts < max_attempts`, и
    снижение лимита делает уже потраченные попытки чрезмерными задним
    числом. Строка при этом остаётся `new` с пустым описанием: её не
    видит НИ ОДНА из трёх выборок и не считает `_warn_about_unscored`
    (там `description IS NOT NULL`). Прогон после такой правки становился
    `ok` — статус улучшался оттого, что работа пропала.
    """
    db = str(tmp_path / "hh.db")
    repository = SqliteRepository(db)
    repository.init_schema()
    mock_source(page=httpx.Response(404))
    three = config_with_attempts(tmp_path / "three", 3)
    for _ in range(2):
        run(three, repository, [RecordingSink()], KeywordScorer(three.profile))
    assert repo_status(db, "111") == ("new", None, 2)

    two = config_with_attempts(tmp_path / "two", 2)
    with caplog.at_level(logging.ERROR):
        stats = run(two, repository, [RecordingSink()], KeywordScorer(two.profile))

    assert (stats.stalled, stats.status) == (1, "partial")
    assert "лимит попыток" in caplog.text
    assert repository.pending_enrichment(2) == []
    repository.close()
    raw = sqlite3.connect(db)
    stalled = [row[0] for row in raw.execute("SELECT stalled FROM run ORDER BY id")]
    raw.close()
    assert stalled == [0, 0, 1]


@respx.mock
def test_unchanged_attempt_limit_never_reports_stalled_rows(
    config: Config, repo: SqliteRepository
) -> None:
    """Обратная сторона: строка, честно исчерпавшая лимит, застрявшей не считается.

    `bump_enrich_attempt` делает её терминальной тем же UPDATE, то есть
    `rejected`/`enrich_failed`, а не невидимым `new`. Иначе сторож
    кричал бы на каждый штатный отказ.
    """
    mock_source(page=httpx.Response(404))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        stats = run(config, repo, [RecordingSink()], scorer)
        assert stats.stalled == 0
    assert run(config, repo, [RecordingSink()], scorer).stalled == 0


# --- I3: потеря вакансии в карантин обязана быть видна в журнале ----------


@respx.mock
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE vacancy SET title = CAST(x'FFFE' AS TEXT) WHERE id = '111'",
        "UPDATE vacancy SET description = CAST(x'FFFE' AS TEXT) WHERE id = '111'",
        "UPDATE vacancy SET published_at = 'не дата' WHERE id = '111'",
        "UPDATE vacancy SET salary_from = 'много' WHERE id = '111'",
        "UPDATE vacancy SET primary_query = CAST(x'FFFE' AS TEXT) WHERE id = '111'",
    ],
)
def test_terminal_quarantine_is_counted_and_degrades_the_run(
    config: Config, db_path: str, sql: str
) -> None:
    """Изоляция порчи работает, а вот наблюдаемость потери — нет.

    Одна битая строка не роняет остальные — это и есть смысл `safe_rows`.
    Но вакансия уходит в `corrupt` НАВСЕГДА, а прогон при этом рапортовал
    `ok`, `error = NULL` и код 0: ни статусом, ни счётчиком, ни причиной
    потеря не отражена. Для очереди пересчёта счётчики `rescored`/`stuck`
    заведены именно ради этого; у карантина симметричного не было.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source()
    run(config, disk, [RecordingSink()])
    corrupt(db_path, "UPDATE vacancy SET status = 'new', reported_at = NULL WHERE id = '111'")
    corrupt(db_path, sql)

    stats = run(config, disk, [RecordingSink()])

    assert (stats.corrupted, stats.status) == (1, "partial")
    assert stats.error is not None and "карантин" in stats.error
    disk.close()
    raw = sqlite3.connect(db_path)
    row = raw.execute(
        "SELECT status, corrupted, error FROM run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    raw.close()
    assert row[0] == "partial" and row[1] == 1 and row[2] is not None


@respx.mock
def test_a_clean_run_reports_no_quarantine(config: Config, repo: SqliteRepository) -> None:
    """Счётчик карантина не имеет права срабатывать на здоровой базе."""
    mock_source()
    stats = run(config, repo, [RecordingSink()])
    assert (stats.corrupted, stats.status) == (0, "ok")


# --- RunStats.degrade: правило «статус только ухудшается» ------------------


def test_status_never_improves() -> None:
    """Шагов, способных частично отказать, четыре, и каждый пишет своё.

    Если бы `degrade` умел улучшать статус, последний шаг затирал бы
    жалобы предыдущих, и `ok` после `failed` означал бы прогон, который
    потерял работу и об этом не сказал. Проверено неэквивалентностью:
    пустая выдача плюс бэклог с недоступными страницами дают `failed` и
    код 1, а с улучшающим `degrade` — `partial` и код 3, то есть успех
    для healthcheck и C2 заново.
    """
    stats = RunStats()
    stats.degrade("failed", "источник не отдал ни одной страницы")
    stats.degrade("partial", "страница вакансии не получена")
    stats.degrade("ok", "всё хорошо")
    assert (stats.status, stats.exit_code()) == ("failed", 1)
    assert stats.error == "источник не отдал ни одной страницы"


def test_the_reason_stays_with_the_worst_status() -> None:
    """При равном статусе побеждает ПЕРВАЯ жалоба.

    Она обычно и есть корень, а последующие — следствия. Забирая причину
    у последней, журнал прогона показывал бы симптом вместо диагноза.
    """
    stats = RunStats()
    stats.degrade("partial", "первая")
    stats.degrade("partial", "вторая")
    assert stats.error == "первая"
    stats.degrade("failed", "корень")
    stats.degrade("failed", "следствие")
    assert (stats.status, stats.error) == ("failed", "корень")


# --- условный запрос: валидатор обязан ДОЕХАТЬ до источника ---------------


@respx.mock
def test_listing_request_carries_the_stored_validators(
    config: Config, repo: SqliteRepository
) -> None:
    """Состояние `http_cache` сторожили, а сам заголовок — нет.

    Валидатор, который никуда не уходит, не экономит ничего: источник
    каждый раз отдаёт полный ответ, а тесты остаются зелёными, потому что
    смотрят в базу, а не в запрос. Здесь проверяется именно исходящий
    запрос.
    """
    repo.save_cache_headers(LISTING_URL, '"v1"', "Wed, 01 Jul 2026 00:00:00 GMT")
    _, listing_route, _ = mock_source()

    run(config, repo, [RecordingSink()])

    headers = listing_route.calls.last.request.headers
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == "Wed, 01 Jul 2026 00:00:00 GMT"


# --- I1: `Repository` как настоящая точка расширения (спека §4.2) ----------
#
# Спека обещает, что `PostgresRepository` не потребует правок в конвейере.
# До появления `storage/base.py` обещание было неисполнимым по типам:
# конвейер был аннотирован конкретным `SqliteRepository`, и `mypy --strict`
# — часть ворот проекта — отвергал ЛЮБУЮ альтернативную реализацию, как бы
# безупречно та ни работала в рантайме:
#
#     error: Argument 3 to "run_once" has incompatible type "MemoryRepository";
#            expected "SqliteRepository"
#
# Поэтому проверка здесь двойная и обе половины обязательны. Рантайм —
# полный прогон на реализации, у которой нет ни строчки SQL. Типы — сам
# факт, что этот файл проходит `mypy --strict`: вызов `run_once` ниже
# передаёт `MemoryRepository` туда, где объявлен протокол, и `_as_protocol`
# фиксирует совместимость явно.


@dataclass
class _MemoryRow:
    discovered: DiscoveredVacancy
    cluster: str
    weight: int
    status: str = STATUS_NEW
    details: VacancyDetails | None = None
    score: ScoreBreakdown | None = None
    attempts: int = 0
    reject_code: str | None = None
    reported_at: datetime | None = None


class MemoryRepository:
    """Хранилище целиком в `dict` — альтернативная реализация протокола.

    Написана ради одного вопроса: правда ли конвейер зависит только от
    интерфейса. Никакого SQL, никакого sqlite3, никакого файла.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _MemoryRow] = {}
        self.cache: dict[str, tuple[str | None, str | None]] = {}
        self.runs: list[dict[str, object]] = []

    # --- discovery ---------------------------------------------------

    def add_discovered(self, vacancy: DiscoveredVacancy, cluster: str, weight: int) -> bool:
        row = self.rows.get(vacancy.id)
        if row is None:
            self.rows[vacancy.id] = _MemoryRow(vacancy, cluster, weight)
            return True
        if weight > row.weight:
            row.cluster, row.weight = cluster, weight
        return False

    def cache_headers(self, url: str) -> dict[str, str]:
        etag, modified = self.cache.get(url, (None, None))
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        return headers

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self.cache[url] = (etag, last_modified)

    def reset_cache(self, url: str) -> None:
        self.cache.pop(url, None)

    # --- префильтр -----------------------------------------------------

    def _queue(self, max_attempts: int) -> list[_MemoryRow]:
        return [
            row
            for row in self.rows.values()
            if row.status == STATUS_NEW and row.details is None and row.attempts < max_attempts
        ]

    def pending_titles(self, max_attempts: int) -> list[tuple[str, str]]:
        return [(row.discovered.id, row.discovered.title) for row in self._queue(max_attempts)]

    def mark_rejected(self, vacancy_id: str, reason: str, code: str) -> None:
        row = self.rows[vacancy_id]
        row.status, row.reject_code = STATUS_REJECTED, code

    def rejected_by_prefilter(self) -> list[tuple[str, str]]:
        return [
            (row.discovered.id, row.discovered.title)
            for row in self.rows.values()
            if row.status == STATUS_REJECTED and row.reject_code == REJECT_CODE_PREFILTER
        ]

    def requeue_prefiltered(self, ids: Sequence[str]) -> int:
        returned = 0
        for vacancy_id in ids:
            row = self.rows[vacancy_id]
            if row.status == STATUS_REJECTED and row.reject_code == REJECT_CODE_PREFILTER:
                row.status, row.reject_code, row.attempts = STATUS_NEW, None, 0
                returned += 1
        return returned

    # --- обогащение ----------------------------------------------------

    def pending_enrichment(self, max_attempts: int, limit: int) -> list[DiscoveredVacancy]:
        return [row.discovered for row in self._queue(max_attempts)][:limit]

    def bump_enrich_attempt(self, vacancy_id: str, max_attempts: int) -> int:
        row = self.rows[vacancy_id]
        row.attempts += 1
        if row.attempts >= max_attempts and row.status == STATUS_NEW:
            row.status, row.reject_code = STATUS_REJECTED, REJECT_CODE_ENRICH_FAILED
        return row.attempts

    def save_description(self, vacancy_id: str, details: VacancyDetails) -> None:
        self.rows[vacancy_id].details = details

    def save_enriched(
        self, vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown
    ) -> None:
        row = self.rows[vacancy_id]
        row.details, row.score = details, score

    def stalled_by_attempts(self, max_attempts: int) -> int:
        return sum(
            1
            for row in self.rows.values()
            if row.status == STATUS_NEW and row.details is None and row.attempts >= max_attempts
        )

    def stalled_rows_hint(self, max_attempts: int) -> str:
        return f"MemoryRepository.rows: status=new, details=None, attempts >= {max_attempts}"

    # --- пересчёт ------------------------------------------------------

    def _unscored(self) -> list[_MemoryRow]:
        return [
            row
            for row in self.rows.values()
            if row.status == STATUS_NEW and row.details is not None and row.score is None
        ]

    def pending_scoring(self, limit: int) -> list[tuple[DiscoveredVacancy, VacancyDetails]]:
        tasks = []
        for row in self._unscored()[:limit]:
            assert row.details is not None
            tasks.append((row.discovered, row.details))
        return tasks

    def count_pending_scoring(self) -> int:
        return len(self._unscored())

    def save_score(self, vacancy_id: str, score: ScoreBreakdown) -> None:
        self.rows[vacancy_id].score = score

    # --- отчёт ---------------------------------------------------------

    def unreported(self, limit: int) -> list[ScoredVacancy]:
        ready = [
            row
            for row in self.rows.values()
            if row.status == STATUS_NEW and row.details is not None and row.score is not None
        ]
        ready.sort(key=lambda row: row.score.total if row.score else 0.0, reverse=True)
        result = []
        for row in ready[:limit]:
            assert row.details is not None and row.score is not None
            result.append(
                ScoredVacancy(
                    discovered=row.discovered,
                    details=row.details,
                    score=row.score,
                    cluster=row.cluster,
                )
            )
        return result

    def mark_reported(self, ids: Sequence[str]) -> None:
        for vacancy_id in ids:
            row = self.rows[vacancy_id]
            row.status, row.reported_at = STATUS_REPORTED, datetime.now(UTC)

    # --- журнал --------------------------------------------------------

    def start_run(self) -> int:
        self.runs.append({"status": "running"})
        return len(self.runs)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        self.runs[run_id - 1] = {"status": status, "finished_at": finished_at, **counters}

    def corrupted_count(self) -> int:
        return 0


def _as_protocol(repo: Repository) -> Repository:
    """Совместимость с протоколом, зафиксированная для `mypy --strict`."""
    return repo


@respx.mock
def test_run_once_accepts_any_repository(config: Config) -> None:
    """Прогон целиком на реализации без единой строчки SQL.

    Дифференциально проверено на FIX_BASE: рантайм проходил там ТОЖЕ (в
    том и была коварность находки), а `mypy --strict` отвергал вызов
    `run_once` с этим типом. Значит зелёный рантайм здесь ничего бы не
    доказал в одиночку — доказательством служит пара «этот тест проходит»
    и «этот файл проходит `mypy --strict`».
    """
    mock_source(TWO_ENRICHABLE)
    repo = MemoryRepository()
    sink = RecordingSink()

    with make_client(config) as client:
        stats = run_once(config, client, repo, KeywordScorer(config.profile), [sink], NOW)

    assert _as_protocol(repo) is repo
    assert stats.status == "ok"
    assert (stats.discovered, stats.new_count, stats.enriched, stats.reported) == (2, 2, 2, 2)
    assert sorted(sink.seen) == ["111", "222"]
    assert all(row.status == STATUS_REPORTED for row in repo.rows.values())
    assert repo.runs[0]["status"] == "ok"


# --- M3: `discovered` считал одну вакансию дважды -------------------------


@respx.mock
def test_a_vacancy_repeated_on_the_next_page_is_counted_once(tmp_path: Path) -> None:
    """Пагинация hh.ru сдвигается между запросами — повтор на границе обычен.

    Выдача живая: пока идёт обход, вакансии добавляются и снимаются, и та
    же запись съезжает со страницы 0 на страницу 1. `parse_listing` ловит
    повтор только ВНУТРИ страницы, поэтому `discovered` считал одну
    вакансию дважды. `new_count` при этом всегда был верен, из-за чего
    расхождение выглядело как «нашли двоих, новый один» — то есть как
    нормальная работа дедупликации, а не как ошибка счёта.
    """
    config = load_config(
        write_config(tmp_path, **{"queries.yaml": ONE_PAGE.replace("pages: 1", "pages: 2")})
    )
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    pages = {
        LISTING_URL: listing_html(("111", "Senior Embedded Engineer")),
        f"{LISTING_URL}?page=1": listing_html(
            ("111", "Senior Embedded Engineer"), ("222", "Middle Python Developer")
        ),
    }
    mock_robots()
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    respx.get(url__startswith=LISTING_URL).mock(
        side_effect=lambda request: httpx.Response(200, text=pages[str(request.url)])
    )

    stats = run(config, repository, [RecordingSink()])

    assert (stats.discovered, stats.new_count) == (2, 2)


@respx.mock
def test_the_same_vacancy_in_two_listings_is_two_findings(tmp_path: Path) -> None:
    """Контроль: дедупликация — на листинг, а не на прогон.

    Две находки одной вакансии двумя разными запросами — это факт о
    вакансии, и именно он решает, какой кластер ей достанется (побеждает
    больший `weight`). Схлопнув их, `discovered` перестал бы отвечать на
    вопрос «сколько раз мы её встретили».
    """
    two_listings = """
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
    pages: 1
  - slug: devops
    cluster: backend
    weight: 3
    pages: 1
"""
    config = load_config(write_config(tmp_path, **{"queries.yaml": two_listings}))
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    mock_robots()
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    respx.get(url__startswith="https://hh.ru/vacancies/").mock(
        side_effect=lambda request: httpx.Response(
            200,
            text=listing_html(
                ("111", "Senior Embedded Engineer"), slug=str(request.url).rsplit("/", 1)[-1]
            ),
        )
    )

    stats = run(config, repository, [RecordingSink()])

    assert (stats.discovered, stats.new_count) == (2, 1)


# --- M7: пустой хвост пагинации рядом с 304 — не отказ прогона -------------


@respx.mock
def test_an_empty_tail_next_to_a_304_does_not_fail_the_run(tmp_path: Path) -> None:
    """`pages: 2`, страница 0 — `304`, страница 1 — пустой хвост.

    Сторож «отдал страницы, вакансий ноль» помечал такой прогон `failed`,
    хотя источник демонстративно жив: он ответил `304`, то есть страницу
    узнал и сказал «не изменилась». `failed` красит healthcheck, и цена
    ошибки тут — красный индикатор на исправном сервисе.

    Дрейф формата этой веткой не прячется: он отказывает РАЗБОРОМ, а
    отказ разбора сбрасывает валидатор, поэтому следующий прогон получает
    полный ответ, `unchanged` становится нулём и сторож срабатывает.
    """
    config = load_config(
        write_config(tmp_path, **{"queries.yaml": ONE_PAGE.replace("pages: 1", "pages: 2")})
    )
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    repository.save_cache_headers(LISTING_URL, '"v1"', None)
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(
        side_effect=lambda request: (
            httpx.Response(304)
            if str(request.url) == LISTING_URL
            else httpx.Response(200, text=listing_html())
        )
    )

    stats = run(config, repository, [RecordingSink()])

    assert (stats.status, stats.discovered) == ("ok", 0)


@respx.mock
def test_without_a_304_an_empty_listing_still_fails_the_run(config: Config) -> None:
    """Контроль: ослабление сторожа не затронуло случай, ради которого он есть."""
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(200, text=listing_html())
    )

    stats = run(config, repository, [RecordingSink()])

    assert stats.status == "failed"


# --- I2: потолок работы прогона доезжает до сети и до отсева ---------------


@respx.mock
def test_enrich_per_run_caps_requests_to_vacancy_pages(tmp_path: Path) -> None:
    """Потолок обогащения — это прямой потолок запросов к hh.ru за прогон.

    Без него накопленный бэклог означал прогон длиной в часы: замер на
    50 листингах по 20 страниц — 20 990 запросов и 5.8 ч одних только
    пауз вежливости при `interval_hours: 4`.
    """
    app_yaml = APP_YAML.replace(
        "enrich:\n  max_attempts: 3\n", "enrich:\n  max_attempts: 3\nlimits:\n  enrich_per_run: 1\n"
    )
    config = load_config(write_config(tmp_path, **{"app.yaml": app_yaml, "queries.yaml": ONE_PAGE}))
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    _, _, page_route = mock_source(TWO_ENRICHABLE)

    stats = run(config, repository, [RecordingSink()])

    assert len(page_route.calls) == 1
    assert stats.enriched == 1
    # Отложенное не потеряно: следующий прогон возьмёт вторую вакансию.
    assert len(repository.pending_enrichment(max_attempts=3, limit=10)) == 1


@respx.mock
def test_prefilter_still_covers_the_queue_beyond_the_enrich_ceiling(tmp_path: Path) -> None:
    """Отсев обязан пройти по ВСЕЙ очереди, а не по окну обогащения.

    Иначе вакансия, не поместившаяся в окно отсева, но попавшая в окно
    обогащения после чужих отказов, ушла бы в сеть, ни разу не пройдя
    единственный барьер перед ней. Здесь потолок обогащения — одна
    вакансия, а стоп-слово стоит у второй.
    """
    app_yaml = APP_YAML.replace(
        "enrich:\n  max_attempts: 3\n", "enrich:\n  max_attempts: 3\nlimits:\n  enrich_per_run: 1\n"
    )
    config = load_config(write_config(tmp_path, **{"app.yaml": app_yaml, "queries.yaml": ONE_PAGE}))
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    mock_source(TWO_VACANCIES)  # у второй вакансии в заголовке «Junior»

    stats = run(config, repository, [RecordingSink()])

    assert stats.rejected == 1
    assert [vacancy_id for vacancy_id, _ in repository.rejected_by_prefilter()] == ["222"]
