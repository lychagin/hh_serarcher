import gzip
import json
import logging
import sqlite3
from collections.abc import Sequence
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
from hh_search.pipeline.stats import RunCounters
from hh_search.scoring.base import Scorer
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository
from hh_search.storage.run_log import ALLOWED_RUN_COUNTERS
from tests.test_config import write_config

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

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if self._fail:
            raise RuntimeError(f"приёмник {self.name} недоступен")
        self.batches.append([item.discovered.id for item in vacancies])
        self.items.extend(vacancies)

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
def test_forbidden_stops_the_run_and_closes_the_journal(config: Config, db_path: str) -> None:
    """403 останавливает прогон (спека §9), но строку журнала закрывает.

    Незакрытая строка `running` — это не косметика: healthcheck смотрит в
    журнал, и висящие строки копятся вечно.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_robots()
    respx.get(url__startswith=LISTING_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(AccessForbidden):
        run(config, disk, [RecordingSink()])
    assert disk.last_successful_run() is None
    assert journal(db_path) == [("failed", 0, 0, 0, 0, 0, 0, 0)]
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
def test_more_than_half_failed_pages_raise_the_canary(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Канарейка на смену вёрстки (спека §9): страница без JSON-LD.

    Причина отказа здесь — сама страница, поэтому в логе обязана быть
    вёрстка, а не недоступность источника: лечит их разный человек.
    """
    mock_source(page="<html>без всякого JSON-LD</html>")
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert stats.status == "partial"
    assert "сменил вёрстку" in caplog.text
