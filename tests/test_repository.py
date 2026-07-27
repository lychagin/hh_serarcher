import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from hh_search.domain.models import DiscoveredVacancy, Salary, ScoreBreakdown, VacancyDetails
from hh_search.storage.repository import SqliteRepository


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def make_vacancy(vacancy_id: str = "1", query: str = "Yocto") -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=vacancy_id,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
        title="Embedded Linux Engineer",
        company="ООО Ромашка",
        area="Нижний Новгород",
        salary=Salary(raw="от 200 000 руб.", amount_from=200000, currency="руб."),
        published_at=datetime(2026, 7, 27, 9, 0, 0),
        found_by_query=query,
    )


def make_score(total: float = 87.4) -> ScoreBreakdown:
    return ScoreBreakdown(
        title=1.0,
        stack=0.8,
        responsibilities=0.67,
        domain=1.0,
        penalty=0.0,
        total=total,
        matched={"stack": ["Yocto"]},
    )


def test_add_discovered_reports_new_only_once(repo: SqliteRepository) -> None:
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is True
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is False


def test_known_ids_returns_only_stored(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    assert repo.known_ids(["1", "2"]) == {"1"}


def test_heavier_query_wins_the_cluster(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy("1", "Yocto"), "embedded", 5)
    repo.add_discovered(make_vacancy("1", "Backend Team Lead"), "backend", 10)
    repo.save_enriched("1", VacancyDetails(description="текст"), make_score())
    assert repo.unreported()[0].cluster == "backend"


def test_rejected_vacancy_is_not_offered_for_enrichment(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.mark_rejected("1", "stop-word in title")
    assert repo.pending_enrichment(max_attempts=3) == []


def test_pending_enrichment_skips_exhausted_attempts(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    for _ in range(3):
        repo.bump_enrich_attempt("1")
    assert repo.pending_enrichment(max_attempts=3) == []


def test_enriched_and_scored_vacancy_becomes_unreported(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Требуется Yocto"), make_score())
    pending = repo.unreported()
    assert len(pending) == 1
    assert pending[0].score.total == 87.4
    assert pending[0].discovered.salary.amount_from == 200000


def test_mark_reported_empties_the_queue(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="текст"), make_score())
    repo.mark_reported(["1"])
    assert repo.unreported() == []


def test_run_journal_tracks_last_success(repo: SqliteRepository) -> None:
    assert repo.last_successful_run() is None
    run_id = repo.start_run()
    repo.finish_run(run_id, "ok", discovered=20, new_count=2)
    assert isinstance(repo.last_successful_run(), datetime)


def test_failed_run_does_not_count_as_success(repo: SqliteRepository) -> None:
    run_id = repo.start_run()
    repo.finish_run(run_id, "failed", error="403")
    assert repo.last_successful_run() is None


def test_cache_headers_round_trip(repo: SqliteRepository) -> None:
    assert repo.cache_headers("https://hh.ru/x") == {}
    repo.save_cache_headers("https://hh.ru/x", etag='"abc"', last_modified=None)
    assert repo.cache_headers("https://hh.ru/x") == {"If-None-Match": '"abc"'}


# --- Раунд исправлений 1 ------------------------------------------------


def test_save_enriched_has_no_partial_state_on_failure(tmp_path: object) -> None:
    """C1: крах во время записи обогащения не должен оставлять вакансию в
    состоянии «описание есть, оценки нет» — невидимом ни для
    pending_enrichment (description уже не NULL), ни для unreported
    (score ещё NULL). Триггер, срабатывающий ровно в момент записи,
    имитирует крах ровно там, где раньше проходила граница между
    save_details и save_score; теперь это один оператор, и он атомарен —
    тригер обязан откатить его целиком."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy(), "embedded", 9)
    repository.close()

    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TRIGGER simulate_crash BEFORE UPDATE OF description ON vacancy "
        "WHEN NEW.score IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'simulated crash mid-write'); END"
    )
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    with pytest.raises(sqlite3.Error):
        repository.save_enriched("1", VacancyDetails(description="text"), make_score())

    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    assert repository.unreported() == []
    repository.close()


def test_unreported_skips_corrupt_score_detail_and_marks_it(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """C2: одна битая строка score_detail не должна блокировать весь отчёт,
    и не должна попадать в очередь снова на каждом следующем прогоне."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score(total=50.0))
    repository.close()

    # Порча данных мимо публичного API репозитория — отдельным сырым
    # соединением, как это могло бы произойти при повреждении диска
    # или при эволюции схемы ScoreBreakdown в будущей задаче.
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE vacancy SET score_detail = ? WHERE id = ?", ("{битый", "2"))
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        result = repository.unreported()

    assert [v.discovered.id for v in result] == ["1"]
    assert "2" in caplog.text

    # битая вакансия получила отдельный статус и не будет всплывать
    # в логе повторно на каждом следующем прогоне
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        repository.unreported()
    assert "2" not in caplog.text
    repository.close()


def test_last_successful_run_normalizes_offsets(repo: SqliteRepository) -> None:
    """I1: разные смещения должны сравниваться как единое время, а не как
    ISO-строки лексикографически."""
    run1 = repo.start_run()
    repo.finish_run(run1, "ok", finished_at=datetime(2026, 7, 27, 23, 0, 0, tzinfo=UTC))
    run2 = repo.start_run()
    # 2026-07-28T01:00:00+03:00 == 2026-07-27T22:00:00 UTC — РАНЬШЕ run1,
    # хотя как голая строка она "больше" из-за даты 07-28.
    repo.finish_run(
        run2,
        "ok",
        finished_at=datetime(2026, 7, 28, 1, 0, 0, tzinfo=timezone(timedelta(hours=3))),
    )
    last = repo.last_successful_run()
    assert last is not None
    assert last.tzinfo is not None
    assert last == datetime(2026, 7, 27, 23, 0, 0, tzinfo=UTC)


def test_finish_run_accepts_naive_datetime_and_returns_aware(repo: SqliteRepository) -> None:
    """I1: наивная дата на входе не должна течь наружу наивной обратно."""
    run_id = repo.start_run()
    repo.finish_run(run_id, "ok", finished_at=datetime(2026, 7, 27, 12, 0, 0))
    last = repo.last_successful_run()
    assert last is not None
    assert last.tzinfo is not None
    assert last == datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def test_found_by_query_matches_the_cluster_winning_query(repo: SqliteRepository) -> None:
    """I2: found_by_query в отчёте обязан совпадать с запросом, который
    определил кластер, — а не с произвольной строкой из vacancy_query."""
    repo.add_discovered(make_vacancy("1", "Yocto"), "embedded", 5)
    repo.add_discovered(make_vacancy("1", "AAA"), "embedded", 1)
    repo.add_discovered(make_vacancy("1", "Backend Team Lead"), "backend", 10)
    repo.save_enriched("1", VacancyDetails(description="текст"), make_score())
    result = repo.unreported()[0]
    assert result.cluster == "backend"
    assert result.discovered.found_by_query == "Backend Team Lead"


def test_save_cache_headers_can_clear_stale_validators(repo: SqliteRepository) -> None:
    """I3: протухший валидатор должен уметь стираться, а не консервироваться
    навсегда ранним return-ом при пустом вызове."""
    repo.save_cache_headers("https://hh.ru/x", etag='"v1"', last_modified=None)
    assert repo.cache_headers("https://hh.ru/x") == {"If-None-Match": '"v1"'}
    repo.save_cache_headers("https://hh.ru/x", etag=None, last_modified=None)
    assert repo.cache_headers("https://hh.ru/x") == {}


def test_reset_cache_removes_stored_validators(repo: SqliteRepository) -> None:
    """I3: явный сброс кэша — аварийный выход, если запрос стабильно
    получает 304 по протухшему валидатору."""
    repo.save_cache_headers("https://hh.ru/x", etag='"v1"', last_modified=None)
    repo.reset_cache("https://hh.ru/x")
    assert repo.cache_headers("https://hh.ru/x") == {}
