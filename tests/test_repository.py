import contextlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    VacancyDetails,
    WorkFormat,
)
from hh_search.storage.base import DEFAULT_BATCH_LIMIT, Housekeeper
from hh_search.storage.migrations import ADDED_COLUMNS
from hh_search.storage.quarantine import STATUS_CORRUPT
from hh_search.storage.repository import (
    REJECT_CODE_ENRICH_FAILED,
    REJECT_CODE_PREFILTER,
    SCHEMA_PATH,
    SqliteRepository,
)
from hh_search.storage.time_utils import to_utc_iso


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


def corrupt(db_path: str, sql: str, *params: object) -> None:
    """Порча данных мимо публичного API — отдельным сырым соединением."""
    raw = sqlite3.connect(db_path)
    raw.execute(sql, params)
    raw.commit()
    raw.close()


def read_column(db_path: str, column: str, vacancy_id: str) -> object:
    raw = sqlite3.connect(db_path)
    row = raw.execute(f"SELECT {column} FROM vacancy WHERE id = ?", (vacancy_id,)).fetchone()
    raw.close()
    return None if row is None else row[0]


def ids_with_status(db_path: str, status: str) -> set[str]:
    """Строки в заданном статусе — сырым SQL, мимо любых выборок."""
    raw = sqlite3.connect(db_path)
    rows = raw.execute("SELECT id FROM vacancy WHERE status = ?", (status,)).fetchall()
    raw.close()
    return {str(row[0]) for row in rows}


def _indexes_of_vacancy(db_path: str) -> set[str]:
    """Явно созданные индексы, принадлежащие именно таблице `vacancy`.

    Имя индекса в SQLite глобально, а `tbl_name` показывает владельца,
    поэтому индекс, уехавший вместе с отодвинутой при миграции таблицей,
    здесь не появится, даже если он ещё существует в базе. Автоиндексы
    первичного ключа отфильтрованы по `sql IS NULL`: они не создавались
    схемой и не могут быть потеряны.
    """
    raw = sqlite3.connect(db_path)
    rows = raw.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'vacancy' AND sql IS NOT NULL"
    ).fetchall()
    raw.close()
    return {str(row[0]) for row in rows}


def test_add_discovered_reports_new_only_once(repo: SqliteRepository) -> None:
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is True
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is False


def test_heavier_query_wins_the_cluster(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy("1", "Yocto"), "embedded", 5)
    repo.add_discovered(make_vacancy("1", "Backend Team Lead"), "backend", 10)
    repo.save_enriched("1", VacancyDetails(description="текст"), make_score())
    assert repo.unreported()[0].cluster == "backend"


def test_rejected_vacancy_is_not_offered_for_enrichment(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.mark_rejected("1", "stop-word in title", REJECT_CODE_PREFILTER)
    assert repo.pending_enrichment(max_attempts=3) == []


def test_pending_enrichment_skips_exhausted_attempts(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    for _ in range(3):
        repo.bump_enrich_attempt("1", max_attempts=3)
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
    assert repository.pending_scoring() == []
    assert repository.unreported() == []
    repository.close()


def test_unreported_skips_corrupt_score_detail_and_requeues_it(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """C2: одна битая строка score_detail (невалидный JSON) не должна
    блокировать весь отчёт и не должна попадать в лог повторно на каждом
    следующем прогоне — после карантина она выпадает из unreported() сама
    (score/score_detail обнулены), без отдельного статуса."""
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

    # битая вакансия ушла в карантин (score/score_detail обнулены) и
    # больше не попадает в unreported() вовсе — не всплывает в логе
    # повторно на каждом следующем прогоне
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        repository.unreported()
    assert "2" not in caplog.text
    repository.close()


def test_null_score_detail_goes_to_pending_scoring(tmp_path: object) -> None:
    """C2 (раунд 2), пересмотрено в раунде 4: score_detail = NULL — это не
    порча, а честное промежуточное состояние «описание есть, оценки нет».
    Раньше такая строка проваливалась мимо обеих выборок (её и лечили
    карантином, роняя весь отчёт по дороге). Теперь у состояния есть
    своя выборка, и вакансия просто ждёт локального пересчёта."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score(total=50.0))
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET score = NULL, score_detail = NULL WHERE id = ?", "2")

    repository = SqliteRepository(db_path)
    assert [v.discovered.id for v in repository.unreported()] == ["1"]
    assert [v.id for v, _ in repository.pending_scoring()] == ["2"]
    assert repository.pending_enrichment(max_attempts=3) == []
    repository.close()


def test_unreported_recovers_from_invalid_utf8_score_detail(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """C2 (раунд 2): score_detail, испорченный на уровне байт (не
    декодируется в UTF-8). sqlite3 декодирует TEXT-колонки при fetch, до
    того как код увидит хоть одну строку, — раньше OperationalError летел
    мимо try, роняя весь курсор, а не только эту строку."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score(total=50.0))
    repository.close()

    raw = sqlite3.connect(db_path)
    # x'FFFEFA...' — заведомо невалидная UTF-8 последовательность,
    # записанная напрямую в TEXT-колонку мимо Python (сам Python никогда
    # не породит такую строку).
    raw.execute(
        "UPDATE vacancy SET score_detail = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE id = ?",
        ("2",),
    )
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        result = repository.unreported()

    assert [v.discovered.id for v in result] == ["1"]
    assert "2" in caplog.text
    repository.close()


def test_corrupt_score_self_heals_into_pending_scoring(tmp_path: object) -> None:
    """C2 (раунд 2), пересмотрено в раунде 4: карантин по-прежнему обязан
    быть самовосстановлением, а не необратимым стоком, — но восстановление
    локальное. Пачка вакансий, у которых разом испортилась оценка
    (например, после эволюции схемы ScoreBreakdown), после ОДНОГО вызова
    unreported() обязана оказаться в pending_scoring и НИ ОДНА — в
    pending_enrichment: описания целы, идти за ними в сеть незачем.
    У вакансии "3" вдобавок накоплены попытки докачки (3 при лимите 99) —
    на локальный пересчёт это не влияет, потому что сеть не
    задействуется. Лимит здесь заведомо не достигнут: с раунда 5 его
    исчерпание переводит вакансию в терминальный `rejected`, и проверять
    на такой строке локальный пересчёт было бы бессмысленно."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="Yocto"), make_score())
    for _ in range(3):
        repository.bump_enrich_attempt("3", max_attempts=99)
    repository.close()

    raw = sqlite3.connect(db_path)
    raw.executemany(
        "UPDATE vacancy SET score_detail = ? WHERE id = ?",
        [("{битый", "1"), ("{битый", "2"), ("{битый", "3")],
    )
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    assert repository.unreported() == []  # весь бэклог испорчен разом

    tasks = repository.pending_scoring()
    assert {v.id for v, _ in tasks} == {"1", "2", "3"}
    assert {details.description for _, details in tasks} == {"Yocto"}
    assert repository.pending_enrichment(max_attempts=99) == []
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


# --- Раунд исправлений 3 ------------------------------------------------


def test_corrupted_discovery_fields_do_not_crash_report(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """N2: порча title/published_at/salary_from (не только score_detail) не
    должна ронять unreported() или pending_enrichment() целиком — модель
    угроз (порча сырым SQL мимо API) касается всех колонок, из которых
    строится DiscoveredVacancy, а не только score_detail."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)  # останется pending
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("3"), "embedded", 9)
    repository.save_enriched("3", VacancyDetails(description="Yocto"), make_score(total=10.0))
    repository.add_discovered(make_vacancy("4"), "embedded", 9)
    repository.save_enriched("4", VacancyDetails(description="Yocto"), make_score(total=20.0))
    repository.close()

    raw = sqlite3.connect(db_path)
    # x'FFFEFA...' — заведомо невалидные UTF-8-байты в title вакансии "1"
    raw.execute(
        "UPDATE vacancy SET title = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE id = ?",
        ("1",),
    )
    raw.execute("UPDATE vacancy SET published_at = 'мусор' WHERE id = ?", ("3",))
    raw.execute("UPDATE vacancy SET salary_from = 'текст' WHERE id = ?", ("4",))
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        pending = repository.pending_enrichment(max_attempts=3)
    assert pending == []  # "1" была единственной pending, и она испорчена

    with caplog.at_level(logging.ERROR):
        unreported = repository.unreported()
    assert {v.discovered.id for v in unreported} == {"2"}
    repository.close()


def test_invalid_utf8_in_numeric_column_does_not_kill_the_cursor(tmp_path: object) -> None:
    """Найдено самоперепроверкой раунда 4: salary_from/salary_to объявлены
    INTEGER и потому не были обёрнуты в CAST(... AS BLOB). Но SQLite
    типизирован динамически — в INTEGER-колонке спокойно лежит текст, в
    том числе невалидный UTF-8, и тогда fetch роняет весь курсор так же,
    как на текстовой колонке. Раунд 3 проверял только валидный текст
    ('текст'), поэтому дыру не увидел."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="Yocto"), make_score())
    repository.close()

    corrupt(
        db_path,
        "UPDATE vacancy SET salary_from = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE id = ?",
        "2",
    )

    repository = SqliteRepository(db_path)
    assert [v.discovered.id for v in repository.unreported()] == ["1"]
    assert read_column(db_path, "status", "2") == STATUS_CORRUPT
    repository.close()


def test_corrupt_payload_is_preserved_after_quarantine(tmp_path: object) -> None:
    """N3: карантин не должен уничтожать улики — исходный (испорченный)
    score_detail сохраняется в corrupt_payload перед обнулением рабочих
    полей, а не теряется безвозвратно (например, если порча на самом деле
    эволюция схемы, которую предстоит мигрировать отдельным скриптом)."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.close()

    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE vacancy SET score_detail = ? WHERE id = ?", ("{битый payload", "1"))
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    assert repository.unreported() == []
    repository.close()

    raw = sqlite3.connect(db_path)
    row = raw.execute("SELECT corrupt_payload FROM vacancy WHERE id = ?", ("1",)).fetchone()
    raw.close()
    assert row is not None
    assert row[0] is not None
    assert row[0].decode("utf-8") == "{битый payload"


def test_model_validate_bug_propagates_instead_of_silently_corrupting(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дополнительно (раунд 3): ошибка в самом валидаторе (не порча
    данных, а баг) обязана падать громко, а не тихо карантинить здоровую
    вакансию и молча возвращать пустой список."""
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())

    def broken_validate(*_args: object, **_kwargs: object) -> ScoreBreakdown:
        raise AttributeError("опечатка в валидаторе")

    monkeypatch.setattr(ScoreBreakdown, "model_validate", broken_validate)
    with pytest.raises(AttributeError):
        repo.unreported()
    monkeypatch.undo()

    # данные здоровой вакансии не должны были быть тихо стёрты карантином
    pending = repo.unreported()
    assert len(pending) == 1
    assert pending[0].discovered.id == "1"


# --- Раунд исправлений 4 ------------------------------------------------

# Схема первого поколения — ровно то, что лежит в персистентном томе на
# VPS у сервиса предыдущей версии: без primary_query, corrupt_payload и
# vacancy_query.weight.
FIRST_GENERATION_SCHEMA = """
CREATE TABLE vacancy (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    area            TEXT,
    salary_raw      TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    salary_currency TEXT,
    published_at    TEXT NOT NULL,
    description     TEXT,
    fetched_at      TEXT,
    enrich_attempts INTEGER NOT NULL DEFAULT 0,
    score           REAL,
    score_detail    TEXT,
    cluster         TEXT,
    cluster_weight  INTEGER NOT NULL DEFAULT 0,
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
    status      TEXT NOT NULL,
    discovered  INTEGER DEFAULT 0,
    new_count   INTEGER DEFAULT 0,
    rejected    INTEGER DEFAULT 0,
    enriched    INTEGER DEFAULT 0,
    reported    INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
"""


def test_init_schema_migrates_database_of_previous_version(tmp_path: object) -> None:
    """C-1: init_schema() — это CREATE TABLE IF NOT EXISTS, поэтому на
    существующей базе он не добавляет новых колонок, и обе выборки падают
    с `no such column`. База персистентна (том на VPS), init_schema
    вызывается при старте — то есть без миграции обновление сервиса
    означает мёртвый сервис."""
    db_path = str(tmp_path) + "/old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(FIRST_GENERATION_SCHEMA)
    raw.execute(
        "INSERT INTO vacancy (id, url, title, published_at, status, first_seen_at) "
        "VALUES ('1', 'https://hh.ru/vacancy/1', 'Старая вакансия', "
        "'2026-07-27T09:00:00+00:00', 'new', '2026-07-27T09:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO vacancy (id, url, title, published_at, status, first_seen_at, "
        "description, score, score_detail) "
        "VALUES ('2', 'https://hh.ru/vacancy/2', 'Оценённая', "
        "'2026-07-27T09:00:00+00:00', 'new', '2026-07-27T09:00:00+00:00', 'Yocto', 87.4, ?)",
        (make_score().model_dump_json(),),
    )
    raw.execute("INSERT INTO vacancy_query (vacancy_id, query) VALUES ('1', 'Yocto')")
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    repository.init_schema()

    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    assert [v.discovered.id for v in repository.unreported()] == ["2"]
    assert repository.pending_scoring() == []
    # миграция идемпотентна: повторный старт сервиса ничего не ломает
    repository.init_schema()
    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    # и новая запись по новой схеме тоже ложится в мигрированную таблицу
    assert repository.add_discovered(make_vacancy("3", "Yocto"), "embedded", 9) is True
    repository.close()


def test_description_is_downloaded_exactly_once_when_score_keeps_breaking(
    tmp_path: object,
) -> None:
    """Главное требование: описание скачивается ровно один раз за всю
    жизнь вакансии. Порча ОЦЕНКИ не должна отправлять нас на hh.ru:
    score_detail вычисляется локально из уже скачанного описания.
    Считаем предложения pending_enrichment — после первого успешного
    скачивания их обязано быть ноль, сколько бы раз ни ломалась оценка."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)

    downloads = 0
    for vacancy in repository.pending_enrichment(max_attempts=3):
        downloads += 1
        repository.save_enriched(vacancy.id, VacancyDetails(description="Yocto"), make_score())
    assert downloads == 1

    for _ in range(5):
        corrupt(db_path, "UPDATE vacancy SET score_detail = ? WHERE id = ?", "{битый", "1")
        assert repository.unreported() == []
        # ключевая проверка: в сеть не предлагается вообще ничего
        downloads += len(repository.pending_enrichment(max_attempts=3))
        assert downloads == 1
        tasks = repository.pending_scoring()
        assert [v.id for v, _ in tasks] == ["1"]
        assert tasks[0][1].description == "Yocto"
        repository.save_score("1", make_score())
        assert [v.discovered.id for v in repository.unreported()] == ["1"]

    assert downloads == 1
    repository.close()


def test_garbage_in_service_column_does_not_crash_report(tmp_path: object) -> None:
    """I-1: int(row["corrupt_count"]) стоял внутри обработчика порчи и сам
    бросал ValueError мимо except при испорченном значении собственной
    служебной колонки — падал весь отчёт. Тот же класс отказа, который
    этот обработчик и закрывал."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score(total=50.0))
    repository.close()

    # Служебная колонка карантина в текущей схеме отсутствует; для базы
    # предыдущего поколения она существует, и тест воспроизводит порчу
    # её значения одинаково в обоих случаях.
    with contextlib.suppress(sqlite3.OperationalError):
        corrupt(db_path, "ALTER TABLE vacancy ADD COLUMN corrupt_count TEXT")
    corrupt(db_path, "UPDATE vacancy SET corrupt_count = 'мусор' WHERE id = ?", "2")
    corrupt(db_path, "UPDATE vacancy SET score_detail = ? WHERE id = ?", "{битый", "2")

    repository = SqliteRepository(db_path)
    assert [v.discovered.id for v in repository.unreported()] == ["1"]
    assert [v.id for v, _ in repository.pending_scoring()] == ["2"]
    repository.close()


def test_corrupted_id_does_not_kill_the_queue_and_the_report(tmp_path: object) -> None:
    """I-4: битый id давал OperationalError при чтении курсора — очередь и
    отчёт были мертвы навсегда, без единой строки в логе. `SELECT CAST(id
    AS BLOB)` читается, а `UPDATE ... WHERE CAST(id AS BLOB) = ?` с
    байтовым параметром адресует такую строку."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("3"), "embedded", 9)
    repository.add_discovered(make_vacancy("4"), "embedded", 9)
    repository.close()

    # разные байтовые последовательности: одинаковые нарушили бы PRIMARY KEY
    corrupt(db_path, "UPDATE vacancy SET id = CAST(x'FFFE01' AS TEXT) WHERE id = ?", "2")
    corrupt(db_path, "UPDATE vacancy SET id = CAST(x'FFFE02' AS TEXT) WHERE id = ?", "4")

    repository = SqliteRepository(db_path)
    assert [v.discovered.id for v in repository.unreported()] == ["1"]
    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["3"]
    # дедупликация тоже переживает нечитаемый ключ: сравнение идёт в
    # SQLite побайтово, до какого-либо декодирования — `add_discovered`
    # отвечает False на уже известный id, не декодируя его
    assert repository.add_discovered(make_vacancy("1"), "embedded", 9) is False
    assert repository.add_discovered(make_vacancy("3"), "embedded", 9) is False
    repository.close()

    raw = sqlite3.connect(db_path)
    terminal = raw.execute(
        "SELECT COUNT(*) FROM vacancy WHERE status = ?", (STATUS_CORRUPT,)
    ).fetchone()[0]
    raw.close()
    assert terminal == 2  # обе строки с нечитаемым ключом адресованы и выведены


def test_quarantine_keeps_first_evidence_and_never_erases_description(
    tmp_path: object,
) -> None:
    """I-2: карантин безусловно перезаписывал corrupt_payload, затирая
    ранее сохранённые улики, и зеркально — обнулял целое description,
    сохраняя в payload валидный score_detail. Улики теперь пишутся
    только первый раз, а description не обнуляется никогда."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())

    corrupt(db_path, "UPDATE vacancy SET score_detail = ? WHERE id = ?", "{первая порча", "1")
    assert repository.unreported() == []
    assert read_column(db_path, "description", "1") == "Yocto"
    payload = read_column(db_path, "corrupt_payload", "1")
    assert isinstance(payload, bytes)
    assert payload.decode("utf-8") == "{первая порча"

    repository.save_score("1", make_score())
    corrupt(db_path, "UPDATE vacancy SET score_detail = ? WHERE id = ?", "{вторая порча", "1")
    assert repository.unreported() == []

    payload = read_column(db_path, "corrupt_payload", "1")
    assert isinstance(payload, bytes)
    assert payload.decode("utf-8") == "{первая порча"
    assert read_column(db_path, "description", "1") == "Yocto"
    repository.close()


def test_repeated_lone_corruptions_do_not_terminate_a_healthy_vacancy(
    tmp_path: object,
) -> None:
    """I-3: corrupt_count не сбрасывался нигде, поэтому три разнесённых во
    времени разовых сбоя, между которыми вакансия успешно обрабатывалась,
    терминировали здоровую вакансию. Счётчика больше нет: локальный
    пересчёт не ходит в сеть, ограничивать нечего."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())

    for _ in range(5):
        corrupt(db_path, "UPDATE vacancy SET score_detail = ? WHERE id = ?", "{битый", "1")
        assert repository.unreported() == []
        assert read_column(db_path, "description", "1") == "Yocto"
        assert [v.id for v, _ in repository.pending_scoring()] == ["1"]
        repository.save_score("1", make_score())
        # между сбоями вакансия каждый раз полностью выздоравливает
        assert [v.discovered.id for v in repository.unreported()] == ["1"]

    assert read_column(db_path, "status", "1") == "new"
    repository.close()


def test_validator_type_error_propagates_instead_of_erasing_data(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-5: TypeError от бага в валидаторе проглатывался и тихо стирал
    оценку здоровой вакансии. Единственным источником TypeError был наш
    собственный raise при score_detail IS NULL — такие строки теперь
    забирает pending_scoring, и TypeError убран из перечня порчи."""
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())

    def broken_validate(*_args: object, **_kwargs: object) -> ScoreBreakdown:
        raise TypeError("баг в валидаторе, а не порча данных")

    monkeypatch.setattr(ScoreBreakdown, "model_validate", broken_validate)
    with pytest.raises(TypeError):
        repo.unreported()
    monkeypatch.undo()

    assert [v.discovered.id for v in repo.unreported()] == ["1"]


def test_discovery_corruption_is_terminal_and_never_redownloaded(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Целевая форма: discovery-колонки пришли из RSS, а не со страницы
    вакансии, — перекачка страницы их не восстановит в принципе.
    Самовосстановление здесь бессмысленно: терминальный статус сразу, с
    одной записью в лог, без обнуления улик и без похода в сеть."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET published_at = 'мусор' WHERE id = ?", "1")

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        assert repository.unreported() == []
    assert len(caplog.records) == 1

    # улики целы: ни одна колонка не обнулена
    assert read_column(db_path, "description", "1") == "Yocto"
    assert read_column(db_path, "published_at", "1") == "мусор"
    assert read_column(db_path, "status", "1") == STATUS_CORRUPT
    assert repository.pending_enrichment(max_attempts=99) == []
    assert repository.pending_scoring() == []
    repository.close()


def test_unreadable_description_is_terminal_and_never_redownloaded(tmp_path: object) -> None:
    """Граница, зафиксированная явно: описание — единственное, что вообще
    можно было бы восстановить перекачкой, и именно поэтому мы этого не
    делаем. Обещание «скачивается ровно один раз» сильнее пользы от
    восстановления одной строки; сама строка остаётся в БД как улика, и
    её всегда можно вернуть в очередь руками через set_status."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.close()

    corrupt(
        db_path,
        "UPDATE vacancy SET description = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE id = ?",
        "1",
    )

    repository = SqliteRepository(db_path)
    assert repository.unreported() == []
    assert repository.pending_enrichment(max_attempts=99) == []
    assert read_column(db_path, "status", "1") == STATUS_CORRUPT
    assert read_column(db_path, "score", "1") == 87.4  # оценка не тронута
    repository.close()


def test_three_selections_partition_the_new_vacancies(tmp_path: object) -> None:
    """Целевая форма целиком: для status = 'new' три выборки не
    пересекаются и вместе покрывают все состояния — ни одно не
    проваливается между ними, как проваливалось «описание есть, оценки
    нет» до этого раунда."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3", "4", "5"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
    for vacancy_id in ("2", "3", "4", "5"):
        repository.save_enriched(vacancy_id, VacancyDetails(description="Yocto"), make_score())
    corrupt(db_path, "UPDATE vacancy SET score = NULL, score_detail = NULL WHERE id = ?", "2")
    repository.mark_rejected("4", "стоп-слово", REJECT_CODE_PREFILTER)
    repository.mark_reported(["5"])

    enrichment = {v.id for v in repository.pending_enrichment(max_attempts=3)}
    scoring = {v.id for v, _ in repository.pending_scoring()}
    reportable = {v.discovered.id for v in repository.unreported()}

    assert enrichment == {"1"}
    assert scoring == {"2"}
    assert reportable == {"3"}
    assert enrichment & scoring == set()
    assert scoring & reportable == set()
    assert enrichment & reportable == set()
    assert enrichment | scoring | reportable == {"1", "2", "3"}
    repository.close()


# --- Раунд исправлений 5 ------------------------------------------------


def test_exhausted_attempts_become_rejected_within_one_call(tmp_path: object) -> None:
    """A-C1: лимит попыток обязан жить ВНУТРИ хранилища.

    Пока `bump_enrich_attempt` и `mark_rejected` были двумя отдельно
    закоммиченными состояниями, между ними существовало состояние
    «status = 'new', enrich_attempts >= max»: pending_enrichment отсекает
    такую строку по счётчику, pending_scoring и unreported — по пустому
    описанию. Вакансия исчезала навсегда, причём без всякой аварии —
    достаточно было, чтобы конвейер не дошёл до второго вызова. Спека
    §5.2 требует rejected/enrich_failed, и теперь это происходит тем же
    оператором, что и инкремент."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)

    assert repository.bump_enrich_attempt("1", max_attempts=3) == 1
    assert read_column(db_path, "status", "1") == "new"
    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]

    assert repository.bump_enrich_attempt("1", max_attempts=3) == 2
    assert read_column(db_path, "status", "1") == "new"

    assert repository.bump_enrich_attempt("1", max_attempts=3) == 3
    # ни одного промежуточного состояния: счётчик и статус меняются вместе
    assert read_column(db_path, "status", "1") == "rejected"
    assert read_column(db_path, "reject_reason", "1") == "enrich_failed"
    assert repository.pending_enrichment(max_attempts=3) == []
    assert repository.pending_scoring() == []
    assert repository.unreported() == []
    repository.close()


def test_every_new_vacancy_is_visible_to_exactly_one_selection(tmp_path: object) -> None:
    """A-C1 + M-7: перебор ВСЕХ достижимых публичным API состояний.

    Проверяется не «три выборки не пересекаются» (это старый тест умел), а
    более сильное: множество строк со status = 'new' в точности равно
    объединению трёх выборок. Именно это равенство нарушала дыра
    A-C1 — строка была 'new' и не входила ни в одну выборку. Все
    состояния создаются публичными вызовами, без единого UPDATE мимо
    API: дыра воспроизводилась ровно так же."""
    db_path = str(tmp_path) + "/states.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    limit = 3
    everything = (
        "нетронутая",
        "попытки-остались",
        "попытки-исчерпаны",
        "описание-без-оценки",
        "описание-и-оценка",
        "отсеяна-префильтром",
        "уже-отправлена",
    )
    for vacancy_id in everything:
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)

    repository.bump_enrich_attempt("попытки-остались", max_attempts=limit)
    for _ in range(limit):
        repository.bump_enrich_attempt("попытки-исчерпаны", max_attempts=limit)
    repository.save_description("описание-без-оценки", VacancyDetails(description="Yocto"))
    repository.save_enriched("описание-и-оценка", VacancyDetails(description="Yocto"), make_score())
    repository.mark_rejected("отсеяна-префильтром", "стоп-слово", REJECT_CODE_PREFILTER)
    repository.save_enriched("уже-отправлена", VacancyDetails(description="Yocto"), make_score())
    repository.mark_reported(["уже-отправлена"])

    enrichment = {v.id for v in repository.pending_enrichment(max_attempts=limit)}
    scoring = {v.id for v, _ in repository.pending_scoring()}
    reportable = {v.discovered.id for v in repository.unreported()}

    assert enrichment == {"нетронутая", "попытки-остались"}
    assert scoring == {"описание-без-оценки"}
    assert reportable == {"описание-и-оценка"}
    assert enrichment & scoring == set()
    assert scoring & reportable == set()
    assert enrichment & reportable == set()
    # ключевое: ни одной 'new' строки за пределами трёх выборок
    assert enrichment | scoring | reportable == ids_with_status(db_path, "new")
    # исчерпавшая попытки не «пропала», а стала терминальной с причиной
    assert read_column(db_path, "status", "попытки-исчерпаны") == "rejected"
    assert read_column(db_path, "reject_reason", "попытки-исчерпаны") == "enrich_failed"
    repository.close()


def test_bump_does_not_resurrect_a_terminal_vacancy(tmp_path: object) -> None:
    """Обратная сторона A-C1: смена статуса внутри инкремента не имеет
    права переписать уже терминальный статус. Строка с нечитаемыми
    discovery-данными (corrupt) — это улика, а не кандидат в rejected."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET status = ? WHERE id = ?", STATUS_CORRUPT, "1")

    repository = SqliteRepository(db_path)
    for _ in range(5):
        repository.bump_enrich_attempt("1", max_attempts=3)
    assert read_column(db_path, "status", "1") == STATUS_CORRUPT
    assert read_column(db_path, "reject_reason", "1") is None
    repository.close()


def test_non_finite_score_is_rejected_at_construction(repo: SqliteRepository) -> None:
    """A-I1: `inf`/`nan` пролезали в ScoreBreakdown без ошибок,
    model_dump_json писал их как `null`, а `null` не проходил обратную
    валидацию при чтении. Запись и чтение не были round-trip: вакансия
    вечно ходила по кругу pending_scoring -> unreported -> карантин, в
    отчёт не попадала никогда и писала ERROR каждый прогон. Достижимо
    опечаткой в YAML (`penalty_per_signal: 1e400`), без порчи базы.
    Починка в корне: нечитаемая оценка не может быть даже создана."""
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            make_score(total=bad)
        with pytest.raises(ValidationError):
            ScoreBreakdown(
                title=1.0,
                stack=bad,
                responsibilities=0.5,
                domain=1.0,
                penalty=0.0,
                total=10.0,
            )
        with pytest.raises(ValidationError):
            ScoreBreakdown.model_validate(
                {
                    "total": bad,
                    "title": 1.0,
                    "stack": 0.5,
                    "responsibilities": 0.5,
                    "domain": 1.0,
                    "penalty": 0.0,
                }
            )

    # круговой прогон обычной оценки цел: то, что записано, читается
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    assert repo.unreported()[0].score == make_score()


def test_save_enriched_keeps_description_when_score_cannot_be_serialized(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-I2: `score.model_dump_json()` вычислялся В КОРТЕЖЕ параметров, то
    есть до UPDATE. Отказ сериализации выбрасывал вместе с оценкой уже
    скачанное описание — и следующий прогон снова шёл на hh.ru за той же
    страницей. Ровно тот сетевой цикл, который убирал раунд 4. Теперь
    описание сохраняется в любом случае, ошибка идёт наружу, а вакансия
    ждёт локального пересчёта."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)

    def broken_dump(*_args: object, **_kwargs: object) -> str:
        raise ValueError("оценка не сериализуется")

    monkeypatch.setattr(ScoreBreakdown, "model_dump_json", broken_dump)
    with pytest.raises(ValueError, match="не сериализуется"):
        repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    monkeypatch.undo()

    assert read_column(db_path, "description", "1") == "Yocto"
    assert read_column(db_path, "score_detail", "1") is None
    # страница скачана и сохранена: в сеть за ней больше не идём
    assert repository.pending_enrichment(max_attempts=3) == []
    assert [v.id for v, _ in repository.pending_scoring()] == ["1"]
    repository.close()


def test_save_description_stores_the_page_without_a_score(repo: SqliteRepository) -> None:
    """Публичный примитив для конвейера (Task 10): скоринг бросил
    исключение — описание уже стоило запроса к hh.ru и обязано пережить
    отказ чисто локального вычисления."""
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_description("1", VacancyDetails(description="Требуется Yocto"))

    tasks = repo.pending_scoring()
    assert [v.id for v, _ in tasks] == ["1"]
    assert tasks[0][1].description == "Требуется Yocto"
    assert repo.pending_enrichment(max_attempts=3) == []
    assert repo.unreported() == []

    repo.save_score("1", make_score())
    assert [v.discovered.id for v in repo.unreported()] == ["1"]


def test_unreported_does_not_accuse_the_pipeline(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """I1: сторож застрявшей очереди убран из `unreported()` — и это не потеря.

    Он говорил неправду и дублировал соседа. Неправду — потому что
    печатал «конвейер не вызвал pending_scoring() перед unreported()»
    ровно в том сценарии, где конвейер зовёт `pending_scoring()` по три
    раза за прогон, а очередь не сходится из-за отказа скорера. Дублировал
    — потому что тот же факт считает `stats.stuck` в
    `pipeline/reporting.py`, и считает лучше: с id, с понижением статуса и
    с записью в журнал прогона (это проверяет
    `test_stuck_scoring_queue_is_reported_once_and_without_a_false_cause`).
    А `unreported()` вызывается дважды за прогон, поэтому один факт
    печатался трижды.

    Выборка при этом не изменилась ни на строку: вакансия без оценки в
    отчёт по-прежнему не попадает.
    """
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.add_discovered(make_vacancy("2"), "embedded", 9)
    repository.save_enriched("2", VacancyDetails(description="Yocto"), make_score(total=50.0))
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET score = NULL, score_detail = NULL WHERE id = ?", "2")

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        result = repository.unreported()

    assert [v.discovered.id for v in result] == ["1"]
    assert "pending_scoring()" not in caplog.text
    assert "конвейер не вызвал" not in caplog.text
    # Строка не пропала и не потерялась: её видит очередь пересчёта.
    assert [vacancy.id for vacancy, _ in repository.pending_scoring()] == ["2"]
    repository.close()


def test_run_journal_accepts_rescoring_counters(tmp_path: object) -> None:
    """Наблюдаемость «очередь пересчёта не сходится» — метрика прогона, а
    не состояние на вакансии. Хранилище обязано её принять; заполнение —
    обязанность конвейера (Task 10)."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    run_id = repository.start_run()
    repository.finish_run(run_id, "ok", discovered=20, rescored=4, stuck=2)
    repository.close()

    raw = sqlite3.connect(db_path)
    row = raw.execute("SELECT rescored, stuck FROM run WHERE id = ?", (run_id,)).fetchone()
    raw.close()
    assert (row[0], row[1]) == (4, 2)


def test_last_successful_run_survives_a_corrupt_journal(tmp_path: object) -> None:
    """M-2: одна битая дата в журнале роняла last_successful_run (ValueError
    на разборе или TypeError на байтах), а от него зависит healthcheck
    (§8.2). Битая строка обязана пропускаться, а не заклинивать сервис
    навсегда: в худшем случае сервис считает себя протухшим, но живёт."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    good = repository.start_run()
    repository.finish_run(good, "ok", finished_at=datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    garbage = repository.start_run()
    repository.finish_run(garbage, "ok", finished_at=datetime(2026, 7, 27, 11, 0, tzinfo=UTC))
    bad_bytes = repository.start_run()
    repository.finish_run(bad_bytes, "ok", finished_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC))
    repository.close()

    # 'мусор' и невалидный UTF-8 сортируются ВЫШЕ валидных дат, поэтому
    # обе битые строки встречаются раньше здоровой.
    corrupt(db_path, "UPDATE run SET finished_at = 'мусор' WHERE id = ?", garbage)
    corrupt(
        db_path,
        "UPDATE run SET finished_at = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE id = ?",
        bad_bytes,
    )

    repository = SqliteRepository(db_path)
    assert repository.last_successful_run() == datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    repository.close()


def test_a_run_finished_in_the_future_is_not_counted_as_successful(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    """M1: часы, ушедшие ВПЕРЁД, делали healthcheck зелёным навсегда.

    `healthcheck` сравнивает `last < deadline`, а дата из будущего меньше
    порога не бывает никогда — значит одна строка журнала с `finished_at`
    на 400 дней вперёд гасит индикатор до самой этой даты. Тот же след
    ломает предохранитель повторного прогона (`_too_soon`): возраст
    прогона отрицательный, то есть «прошлый был только что» — вечно.
    Строка обязана пропускаться, как и нечитаемая, и обязана быть
    объявлена: молча игнорировать след скачка часов — значит скрыть
    единственную улику.
    """
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    real = repository.start_run()
    repository.finish_run(real, "ok", finished_at=datetime.now(UTC) - timedelta(hours=1))
    skewed = repository.start_run()
    repository.finish_run(skewed, "ok", finished_at=datetime.now(UTC) + timedelta(days=400))
    repository.close()

    repository = SqliteRepository(db_path)
    with caplog.at_level(logging.ERROR):
        last = repository.last_successful_run()
    assert last is not None
    assert last < datetime.now(UTC)
    assert "часы уходили вперёд" in caplog.text
    repository.close()


def test_the_last_run_summary_is_readable_even_when_the_journal_is_corrupt(
    tmp_path: object,
) -> None:
    """Счётчики прогона писались всегда, а читать их было некому.

    Единственная выборка из `run` брала `finished_at` успешных прогонов,
    то есть наблюдаемость существовала только на запись. Сводка нужна
    `healthcheck`: человек смотрит именно её, когда индикатор покраснел.
    Падать на порче она не имеет права — за ней приходят как раз тогда,
    когда с данными что-то не так.
    """
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    run_id = repository.start_run()
    repository.finish_run(run_id, "failed", discovered=20, enriched=0, reported=0, error="дрейф")
    repository.close()

    corrupt(db_path, "UPDATE run SET discovered = x'FFFE' WHERE id = ?", run_id)

    repository = SqliteRepository(db_path)
    summary = repository.last_run()
    assert summary is not None
    assert summary.status == "failed"
    assert "дрейф" in summary.describe()
    assert "обогащено 0" in summary.describe()
    repository.close()


def test_cache_headers_never_leak_bytes_into_http(tmp_path: object) -> None:
    """M-2: битый валидатор уезжал в HTTP-заголовок как `bytes` (BLOB в
    колонке) либо ронял fetch целиком (невалидный UTF-8 в TEXT). Значения
    заголовков обязаны быть `str`; нечитаемый валидатор просто не
    отправляется — худший исход полный ответ вместо 304."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    modified = "Wed, 21 Oct 2026 07:28:00 GMT"
    repository.save_cache_headers("https://hh.ru/blob", '"v1"', modified)
    repository.save_cache_headers("https://hh.ru/text", '"v2"', modified)
    repository.close()

    corrupt(db_path, "UPDATE http_cache SET etag = x'FFFEFA' WHERE url = ?", "https://hh.ru/blob")
    corrupt(
        db_path,
        "UPDATE http_cache SET etag = CAST(x'FFFEFA696E76616C6964' AS TEXT) WHERE url = ?",
        "https://hh.ru/text",
    )

    repository = SqliteRepository(db_path)
    for url in ("https://hh.ru/blob", "https://hh.ru/text"):
        headers = repository.cache_headers(url)
        assert headers == {"If-Modified-Since": modified}
        assert all(isinstance(value, str) for value in headers.values())
    repository.close()


def test_migration_backfills_primary_query_and_run_counters(tmp_path: object) -> None:
    """M-3: ALTER TABLE ставит всем старым строкам primary_query = '', и
    отчёт по мигрированной базе показывал бы пустой found_by_query —
    возврат дефекта I2 (рассинхрон с кластером) для всех старых вакансий.
    Реальные запросы лежат в vacancy_query, откуда их и надо взять."""
    db_path = str(tmp_path) + "/old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(FIRST_GENERATION_SCHEMA)
    for vacancy_id in ("1", "2"):
        raw.execute(
            "INSERT INTO vacancy (id, url, title, published_at, status, first_seen_at) "
            "VALUES (?, ?, 'Старая вакансия', '2026-07-27T09:00:00+00:00', 'new', "
            "'2026-07-27T09:00:00+00:00')",
            (vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}"),
        )
    raw.executemany(
        "INSERT INTO vacancy_query (vacancy_id, query) VALUES (?, ?)",
        [("1", "Yocto"), ("2", "Yocto"), ("2", "Embedded Linux")],
    )
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    for _ in range(3):  # идемпотентность: три старта сервиса подряд
        repository.init_schema()

    found = {v.id: v.found_by_query for v in repository.pending_enrichment(max_attempts=3)}
    assert found == {"1": "Yocto", "2": "Embedded Linux"}  # при равных весах — детерминированно

    # новые счётчики прогона тоже доехали до старой базы
    run_id = repository.start_run()
    repository.finish_run(run_id, "ok", rescored=1, stuck=2)
    repository.close()

    raw = sqlite3.connect(db_path)
    row = raw.execute("SELECT rescored, stuck FROM run WHERE id = ?", (run_id,)).fetchone()
    raw.close()
    assert (row[0], row[1]) == (1, 2)


# --- Раунд переезда discovery на листинг ---------------------------------


def make_listed(vacancy_id: str = "1", query: str = "programmist") -> DiscoveredVacancy:
    """Ровно то, что даёт листинг /vacancies/{slug}: id, url, заголовок.

    Ни компании, ни региона, ни зарплаты, ни даты публикации — всё это
    приходит только со страницы вакансии, на шаге обогащения.
    """
    return DiscoveredVacancy(
        id=vacancy_id,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
        title=f"Вакансия {vacancy_id}",
        found_by_query=query,
    )


def enriched_details() -> VacancyDetails:
    """То, что реально приносит живая страница вакансии (см. test_vacancy_page)."""
    return VacancyDetails(
        description="Требуется Yocto",
        published_at=datetime(2026, 7, 27, 19, 27, 20, tzinfo=UTC),
        valid_through=datetime(2026, 8, 5, 19, 27, 20, tzinfo=UTC),
        company="Альтео Софт",
        area="Москва",
        salary=Salary(
            raw="от 100 000 до 150 000 ₽ за месяц на руки",
            amount_from=100000,
            amount_to=150000,
            currency="₽",
        ),
    )


def test_vacancy_without_published_at_is_stored_and_ordered_by_first_seen(
    tmp_path: object,
) -> None:
    """Листинг не отдаёт даты публикации, поэтому между discovery и
    обогащением published_at — NULL. Сортировка `ORDER BY published_at`
    ставила такие строки в непредсказуемое место (в SQLite NULL меньше
    любого значения, и при DESC они уезжали в хвост очереди), а с NOT NULL
    в схеме вставка вообще не проходила. Падаем на first_seen_at."""
    db_path = str(tmp_path) + "/listing.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3"):
        assert repository.add_discovered(make_listed(vacancy_id), "dev", 5) is True
    # first_seen_at ставится репозиторием «сейчас»; задаём его явно, чтобы
    # порядок проверялся детерминированно, а не гонкой микросекунд
    for vacancy_id, seen in (("1", "01"), ("2", "03"), ("3", "02")):
        corrupt(
            db_path,
            "UPDATE vacancy SET first_seen_at = ? WHERE id = ?",
            f"2026-07-{seen}T00:00:00+00:00",
            vacancy_id,
        )

    pending = repository.pending_enrichment(max_attempts=3)
    assert [v.id for v in pending] == ["2", "3", "1"], "свежайшая по first_seen_at — первой"
    assert all(v.published_at is None for v in pending)
    assert read_column(db_path, "published_at", "1") is None
    repository.close()


def test_save_enriched_stores_every_field_the_page_brought(repo: SqliteRepository) -> None:
    """Компания, регион, зарплата и обе даты приходят ОДНОЙ страницей и
    обязаны сохраняться тем же оператором, что описание и оценка: они
    оплачены одним запросом к hh.ru, разъехаться им нечем."""
    repo.add_discovered(make_listed("1"), "embedded", 9)
    repo.save_enriched("1", enriched_details(), make_score())

    scored = repo.unreported()
    assert len(scored) == 1
    discovered = scored[0].discovered
    assert discovered.company == "Альтео Софт"
    assert discovered.area == "Москва"
    assert (discovered.salary.amount_from, discovered.salary.amount_to) == (100000, 150000)
    assert discovered.salary.currency == "₽"
    assert discovered.published_at == datetime(2026, 7, 27, 19, 27, 20, tzinfo=UTC)
    assert scored[0].details.valid_through == datetime(2026, 8, 5, 19, 27, 20, tzinfo=UTC)


def test_page_fields_survive_a_score_that_cannot_be_serialized(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Продолжение A-I2 для полей, приехавших с переездом: отказ чисто
    локальной сериализации оценки не имеет права выбрасывать компанию и
    зарплату — за них заплачено тем же единственным запросом, что и за
    описание."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_listed("1"), "embedded", 9)

    def broken_dump(*_args: object, **_kwargs: object) -> str:
        raise ValueError("оценка не сериализуется")

    monkeypatch.setattr(ScoreBreakdown, "model_dump_json", broken_dump)
    with pytest.raises(ValueError, match="не сериализуется"):
        repository.save_enriched("1", enriched_details(), make_score())
    monkeypatch.undo()

    assert read_column(db_path, "company", "1") == "Альтео Софт"
    assert read_column(db_path, "salary_from", "1") == 100000
    assert read_column(db_path, "score_detail", "1") is None
    assert repository.pending_enrichment(max_attempts=3) == [], "в сеть за страницей не идём"
    repository.close()


def test_enrichment_fills_but_never_erases_what_discovery_already_knew(
    repo: SqliteRepository,
) -> None:
    """«Зарплата не указана» — самый обычный ответ страницы, и присвоение
    NULL затирало бы значение, добытое раньше. У баз, мигрировавших с RSS,
    company/area/salary/published_at заполнены ещё на discovery: обесценить
    их отсутствием блока на странице значит потерять данные без всякой
    ошибки. Перекачки не бывает, поэтому «залипнуть» тут нечему."""
    repo.add_discovered(make_vacancy("1"), "embedded", 9)  # RSS-эпоха: всё заполнено
    repo.save_enriched("1", VacancyDetails(description="Требуется Yocto"), make_score())

    discovered = repo.unreported()[0].discovered
    assert discovered.salary.amount_from == 200000
    # salary_raw проверяется наравне с числовыми полями: без него мутант,
    # снимающий COALESCE именно с этой колонки, выживал — а в отчёт идёт
    # как раз сырая строка, числа лишь сортируют.
    assert discovered.salary.raw == "от 200 000 руб."
    assert discovered.salary.currency == "руб."
    assert discovered.company == "ООО Ромашка"
    assert discovered.area == "Нижний Новгород"
    assert discovered.published_at == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def test_migration_makes_published_at_nullable_without_losing_rows(tmp_path: object) -> None:
    """Схема первого поколения объявляла published_at NOT NULL: RSS отдавал
    дату сразу. Листинг её не отдаёт, поэтому вставка новой вакансии в
    непереехавшую базу падала бы с IntegrityError на каждом прогоне.
    SQLite не умеет ослаблять ограничение через ALTER TABLE, значит
    таблица перестраивается — и обязана донести все строки, значения,
    внешние ключи и индексы."""
    db_path = str(tmp_path) + "/old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(FIRST_GENERATION_SCHEMA)
    raw.executescript("CREATE INDEX IF NOT EXISTS idx_vacancy_status ON vacancy(status);")
    raw.execute(
        "INSERT INTO vacancy (id, url, title, company, area, salary_raw, salary_from, "
        "published_at, status, first_seen_at, cluster, cluster_weight) "
        "VALUES ('1', 'https://hh.ru/vacancy/1', 'Старая вакансия', 'ООО Ромашка', "
        "'Нижний Новгород', 'от 200 000 руб.', 200000, '2026-07-27T09:00:00+00:00', "
        "'new', '2026-07-20T09:00:00+00:00', 'embedded', 9)"
    )
    raw.execute("INSERT INTO vacancy_query (vacancy_id, query) VALUES ('1', 'Yocto')")
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    repository.init_schema()

    # 0. ПЕРВЫЙ же старт обязан оставить базу полностью исправной. Проверка
    #    идёт до повторных вызовов сознательно: индекс, уехавший с
    #    отодвинутой таблицей, восстанавливался бы вторым init_schema, и
    #    потеря была бы не видна — при том что весь первый прогон сервиса
    #    шёл бы по vacancy без индекса.
    assert _indexes_of_vacancy(db_path) >= {"idx_vacancy_status"}

    # 1. старая строка цела во всех своих значениях
    old = repository.pending_enrichment(max_attempts=3)[0]
    assert old.id == "1"
    assert old.company == "ООО Ромашка"
    assert old.area == "Нижний Новгород"
    assert old.salary.amount_from == 200000
    assert old.published_at == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    assert old.found_by_query == "Yocto", "бэкфилл primary_query пережил перестроение"

    # 2. а новая, найденная листингом и БЕЗ даты публикации, теперь ложится
    assert repository.add_discovered(make_listed("2"), "dev", 5) is True
    assert {v.id for v in repository.pending_enrichment(max_attempts=3)} == {"1", "2"}
    repository.close()

    # 3. ограничение снято, внешний ключ цел, индекс не потерян вместе с
    #    отодвинутой таблицей, временной таблицы не осталось
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    notnull = {r["name"]: r["notnull"] for r in raw.execute("PRAGMA table_info(vacancy)")}
    assert notnull["published_at"] == 0
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {
        r[0]
        for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='vacancy'"
        )
    }
    assert "idx_vacancy_status" in indexes, "индекс уехал с отодвинутой таблицей"
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(name.startswith("vacancy_before") for name in tables)
    assert raw.execute("SELECT query FROM vacancy_query").fetchall()[0][0] == "Yocto"
    raw.close()


def test_added_columns_cover_every_column_of_the_current_schema() -> None:
    """M-5: колонка, добавленная в schema.sql без строки в ADDED_COLUMNS,
    делает миграцию молча неполной — и это проявляется только на проде,
    при апгрейде персистентной базы, как `no such column`. Сторож
    сравнивает schema.sql с достижимым множеством «схема первого
    поколения ∪ ADDED_COLUMNS»."""
    current = sqlite3.connect(":memory:")
    current.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    first_generation = sqlite3.connect(":memory:")
    first_generation.executescript(FIRST_GENERATION_SCHEMA)

    def tables(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def columns(connection: sqlite3.Connection, table: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    for table in sorted(tables(current) & tables(first_generation)):
        reachable = columns(first_generation, table) | {
            column for owner, column, _ in ADDED_COLUMNS if owner == table
        }
        missing = columns(current, table) - reachable
        assert missing == set(), (
            f"колонки {sorted(missing)} таблицы {table} есть в schema.sql, но не "
            f"добавляются миграцией: старая база после апгрейда останется без них"
        )
    current.close()
    first_generation.close()


# --- Раунд исправлений 6 --------------------------------------------------
#
# Перестроение таблицы vacancy состоит из трёх шагов, и `executescript`
# между ними неявно коммитит отложенную транзакцию. Поэтому состояние
# «старая таблица отодвинута, новая пустая создана, строки ещё не
# перелиты» ДОЛГОВЕЧНО: оно переживает смерть процесса (OOM-kill на VPS,
# docker stop, SIGKILL) и обязано доигрываться следующим стартом.

LEGACY_VACANCY = "vacancy_before_nullable_published_at"
MIGRATION_ROWS = 3


def _first_generation_with_rows(db_path: str, rows: int = MIGRATION_ROWS) -> None:
    """База предыдущего поколения с живым бэклогом, индексом и внешними ключами."""
    raw = sqlite3.connect(db_path)
    raw.executescript(FIRST_GENERATION_SCHEMA)
    raw.executescript("CREATE INDEX IF NOT EXISTS idx_vacancy_status ON vacancy(status);")
    for number in range(1, rows + 1):
        raw.execute(
            "INSERT INTO vacancy (id, url, title, company, published_at, status, "
            "first_seen_at, cluster, cluster_weight) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(number),
                f"https://hh.ru/vacancy/{number}",
                f"Вакансия {number}",
                "ООО Ромашка",
                "2026-07-27T09:00:00+00:00",
                "new",
                "2026-07-20T09:00:00+00:00",
                "embedded",
                9,
            ),
        )
        raw.execute(
            "INSERT INTO vacancy_query (vacancy_id, query) VALUES (?, 'Yocto')", (str(number),)
        )
    raw.commit()
    raw.close()


def _die_inside_migration(db_path: str, stage: str) -> None:
    """Собрать сырым SQL ровно то состояние, в котором умер процесс.

    Никакого кода миграции здесь не вызывается сознательно: состояние
    описывается тем, что реально лежит на диске, а не тем, как его туда
    положили, — иначе тест сторожил бы реализацию, а не восстановление.
    """
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA legacy_alter_table=ON")
    raw.execute("DROP INDEX idx_vacancy_status")
    raw.execute(f"ALTER TABLE vacancy RENAME TO {LEGACY_VACANCY}")
    raw.execute("PRAGMA legacy_alter_table=OFF")
    if stage in ("новая таблица создана", "перелив начат"):
        raw.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if stage == "перелив начат":
        # Перелив успел скопировать первую строку и умер на второй.
        raw.execute(
            "INSERT INTO vacancy (id, url, title, company, published_at, status, "
            f"first_seen_at, cluster, cluster_weight) SELECT id, url, title, company, "
            "published_at, status, first_seen_at, cluster, cluster_weight "
            f"FROM {LEGACY_VACANCY} WHERE id = '1'"
        )
    raw.commit()
    raw.close()


@pytest.mark.parametrize(
    "stage",
    ["только переименование", "новая таблица создана", "перелив начат"],
)
def test_migration_killed_midway_is_finished_by_the_next_start(
    tmp_path: object, stage: str
) -> None:
    """Смерть процесса посреди перестроения не имеет права уносить таблицу.

    Раньше следующий старт видел, что `vacancy` существует и published_at
    уже nullable, — и не делал НИЧЕГО: строки навсегда оставались в
    отодвинутой таблице, `vacancy` была пуста, внешние ключи висели,
    `apply_schema` возвращался без исключения, а `PRAGMA integrity_check`
    говорил `ok`. Тихая потеря всего бэклога на персистентном томе.
    """
    db_path = str(tmp_path) + "/killed.db"
    _first_generation_with_rows(db_path)
    _die_inside_migration(db_path, stage)

    repository = SqliteRepository(db_path)
    repository.init_schema()

    pending = repository.pending_enrichment(max_attempts=3)
    assert {v.id for v in pending} == {str(n) for n in range(1, MIGRATION_ROWS + 1)}
    assert all(v.company == "ООО Ромашка" for v in pending)
    assert all(v.published_at == datetime(2026, 7, 27, 9, 0, tzinfo=UTC) for v in pending)
    assert all(v.found_by_query == "Yocto" for v in pending), "бэкфилл догнал доигранную миграцию"
    # индекс восстановлен на ПЕРВОМ же старте: он уехал с отодвинутой таблицей
    assert _indexes_of_vacancy(db_path) >= {"idx_vacancy_status"}
    # доигранная миграция идемпотентна: следующий старт ничего не ломает
    repository.init_schema()
    assert len(repository.pending_enrichment(max_attempts=3)) == MIGRATION_ROWS
    repository.close()

    raw = sqlite3.connect(db_path)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert LEGACY_VACANCY not in tables, "отодвинутая таблица обязана исчезнуть"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    notnull = {r[1]: r[3] for r in raw.execute("PRAGMA table_info(vacancy)")}
    assert notnull["published_at"] == 0
    raw.close()


def test_details_are_read_back_exactly_as_they_were_written(repo: SqliteRepository) -> None:
    """`VacancyDetails` — носитель «что принесла страница», и чтение обязано
    отдавать то же самое. Асимметрия здесь тиха и дорога: тип обещает
    company/area/salary/published_at, а приёмник отчёта, взявший
    `details.company`, получал бы пустую колонку без единого предупреждения.
    """
    repo.add_discovered(make_listed("1"), "embedded", 9)
    written = enriched_details()
    repo.save_enriched("1", written, make_score())

    assert repo.unreported()[0].details == written


def test_details_round_trip_through_the_scoring_queue(repo: SqliteRepository) -> None:
    """Та же симметрия на второй выборке, отдающей VacancyDetails."""
    repo.add_discovered(make_listed("1"), "embedded", 9)
    written = enriched_details()
    repo.save_description("1", written)

    _, details = repo.pending_scoring()[0]
    assert details == written


def test_reported_since_takes_only_reported_rows_inside_the_window(tmp_path: object) -> None:
    """`report --since N` — выборка по окну, а не «всё, что есть».

    Проверяются обе границы сразу: неотправленная вакансия в отчёт не
    попадает (её отправит конвейер, и повтор был бы задвоением), а
    отправленная раньше окна — не попадает тоже, иначе `--since` не значит
    ничего.
    """
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="текст"), make_score())
    repository.mark_reported(["1", "2"])
    repository.close()
    long_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    corrupt(db_path, "UPDATE vacancy SET reported_at = ? WHERE id = '2'", long_ago)

    repository = SqliteRepository(db_path)
    window = datetime.now(UTC) - timedelta(days=7)
    assert [item.discovered.id for item in repository.reported_since(window)] == ["1"]
    repository.close()


# --- Раунд исправлений 7: отказ префильтра перестаёт быть вечным ----------
#
# Один список `negative` обслуживал два механизма с несопоставимой ценой
# ошибки: в скоринге совпадение стоит штраф и вакансия остаётся в отчёте,
# а в префильтре то же слово означало `status='rejected'` НАВСЕГДА. Решение
# о префильтре при этом чисто локальное и бесплатное — заголовок лежит в
# базе, сеть не нужна вовсе, — поэтому необратимость снята.


def test_prefilter_rejection_is_told_apart_from_enrich_failure_by_a_code(
    tmp_path: object,
) -> None:
    """Различие машинное, а не по тексту причины.

    Разбор `reject_reason` по префиксу вернул бы ровно тот класс тихого
    отказа, против которого написан весь модуль: текст причины
    перечисляет совпавшие стоп-слова и будет меняться, а разъехавшийся
    префикс молча изменил бы множество возвращаемых вакансий.
    """
    db_path = str(tmp_path) + "/codes.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("отсеяна", "не-скачалась"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
    repository.mark_rejected("отсеяна", "стоп-слово в заголовке: курьер", REJECT_CODE_PREFILTER)
    for _ in range(3):
        repository.bump_enrich_attempt("не-скачалась", max_attempts=3)

    assert read_column(db_path, "reject_code", "отсеяна") == REJECT_CODE_PREFILTER
    assert read_column(db_path, "reject_code", "не-скачалась") == REJECT_CODE_ENRICH_FAILED
    # и текст причины остаётся человеческим, не подменяя собой код
    assert read_column(db_path, "reject_reason", "отсеяна") == "стоп-слово в заголовке: курьер"
    assert [key for key, _ in repository.rejected_by_prefilter()] == ["отсеяна"]
    repository.close()


def test_requeue_returns_the_vacancy_into_the_enrichment_queue(tmp_path: object) -> None:
    """Возврат стирает след отказа и делает вакансию видимой очереди."""
    db_path = str(tmp_path) + "/back.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.mark_rejected("1", "стоп-слово в заголовке: курьер", REJECT_CODE_PREFILTER)
    assert repository.pending_enrichment(max_attempts=3) == []

    assert repository.requeue_prefiltered(["1"]) == 1

    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    assert read_column(db_path, "status", "1") == "new"
    assert read_column(db_path, "reject_reason", "1") is None
    assert read_column(db_path, "reject_code", "1") is None
    # повторный возврат уже ничего не находит: охрана WHERE совпадает с выборкой
    assert repository.requeue_prefiltered(["1"]) == 0
    assert repository.rejected_by_prefilter() == []
    repository.close()


def test_requeue_restores_the_attempt_budget(tmp_path: object) -> None:
    """Возврат с исчерпанным счётчиком воспроизвёл бы Critical спеки §5.2.

    Вакансия может израсходовать попытки скачивания ДО того, как правка
    конфига её отбракует. Возврат в `new` без обнуления счётчика дал бы
    `status='new'`, `description IS NULL`, `enrich_attempts >= max` —
    состояние, невидимое ВСЕМ трём выборкам, то есть вакансия исчезла бы
    ещё тише, чем отказом префильтра.
    """
    db_path = str(tmp_path) + "/budget.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    limit = 3
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    for _ in range(limit - 1):
        repository.bump_enrich_attempt("1", max_attempts=limit)
    repository.mark_rejected("1", "стоп-слово в заголовке: курьер", REJECT_CODE_PREFILTER)

    assert repository.requeue_prefiltered(["1"]) == 1

    assert read_column(db_path, "enrich_attempts", "1") == 0
    assert [v.id for v in repository.pending_enrichment(max_attempts=limit)] == ["1"]
    assert ids_with_status(db_path, "new") == {"1"}
    repository.close()


def test_requeue_resurrects_nothing_but_a_prefilter_rejection(tmp_path: object) -> None:
    """Охрана WHERE, а не доверие вызывающему.

    Возврат по списку id обязан быть безопасен даже при неверном списке:
    `enrich_failed` имеет другой смысл (страница не разбирается, и
    заголовок про это ничего не знает), ручной отказ — чужое решение,
    `corrupt` и `reported` терминальны.
    """
    db_path = str(tmp_path) + "/guard.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    everything = ("не-скачалась", "отказ-человека", "испорчена", "отправлена")
    for vacancy_id in everything:
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
    for _ in range(3):
        repository.bump_enrich_attempt("не-скачалась", max_attempts=3)
    repository.set_status("отказ-человека", "rejected")
    repository.save_enriched("отправлена", VacancyDetails(description="Yocto"), make_score())
    repository.mark_reported(["отправлена"])
    repository.close()
    corrupt(db_path, "UPDATE vacancy SET status = ? WHERE id = ?", STATUS_CORRUPT, "испорчена")

    repository = SqliteRepository(db_path)
    assert repository.rejected_by_prefilter() == []
    assert repository.requeue_prefiltered(list(everything)) == 0

    assert read_column(db_path, "status", "не-скачалась") == "rejected"
    assert read_column(db_path, "reject_reason", "не-скачалась") == "enrich_failed"
    assert read_column(db_path, "status", "отказ-человека") == "rejected"
    assert read_column(db_path, "status", "испорчена") == STATUS_CORRUPT
    assert read_column(db_path, "status", "отправлена") == "reported"
    repository.close()


def test_three_selections_still_partition_the_new_vacancies_after_a_requeue(
    tmp_path: object,
) -> None:
    """Инвариант трёх выборок переживает возврат — перебором состояний.

    Возвращаются строки в двух разных состояниях сразу: без описания
    (обычный случай — отсев стоит ДО скачивания) и с описанием, которое
    было записано раньше. Первая обязана оказаться в `pending_enrichment`,
    вторая — в `pending_scoring`; ни одна не имеет права выпасть мимо
    всех трёх.
    """
    db_path = str(tmp_path) + "/partition.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    limit = 3
    everything = (
        "нетронутая",
        "попытки-остались",
        "попытки-исчерпаны",
        "описание-без-оценки",
        "описание-и-оценка",
        "уже-отправлена",
        "вернётся-без-описания",
        "вернётся-с-описанием",
        "останется-отсеянной",
    )
    for vacancy_id in everything:
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
    repository.bump_enrich_attempt("попытки-остались", max_attempts=limit)
    for _ in range(limit):
        repository.bump_enrich_attempt("попытки-исчерпаны", max_attempts=limit)
    repository.save_description("описание-без-оценки", VacancyDetails(description="Yocto"))
    repository.save_enriched("описание-и-оценка", VacancyDetails(description="Yocto"), make_score())
    repository.save_enriched("уже-отправлена", VacancyDetails(description="Yocto"), make_score())
    repository.mark_reported(["уже-отправлена"])
    repository.save_description("вернётся-с-описанием", VacancyDetails(description="Yocto"))
    for vacancy_id in ("вернётся-без-описания", "вернётся-с-описанием", "останется-отсеянной"):
        repository.mark_rejected(vacancy_id, "стоп-слово", REJECT_CODE_PREFILTER)

    returning = ["вернётся-без-описания", "вернётся-с-описанием"]
    assert repository.requeue_prefiltered(returning) == 2

    enrichment = {v.id for v in repository.pending_enrichment(max_attempts=limit)}
    scoring = {v.id for v, _ in repository.pending_scoring()}
    reportable = {v.discovered.id for v in repository.unreported()}

    assert enrichment == {"нетронутая", "попытки-остались", "вернётся-без-описания"}
    assert scoring == {"описание-без-оценки", "вернётся-с-описанием"}
    assert reportable == {"описание-и-оценка"}
    assert enrichment & scoring == set()
    assert scoring & reportable == set()
    assert enrichment & reportable == set()
    # ключевое: ни одной 'new' строки за пределами трёх выборок
    assert enrichment | scoring | reportable == ids_with_status(db_path, "new")
    assert ids_with_status(db_path, "rejected") == {"попытки-исчерпаны", "останется-отсеянной"}
    repository.close()


def test_migration_makes_the_accumulated_prefilter_rejections_reversible(
    tmp_path: object,
) -> None:
    """База прошлого поколения: кода нет ни у одного отказа.

    Решение — считать накопленные отказы с причиной отказами ПРЕФИЛЬТРА,
    то есть обратимыми: бэклог как раз и состоит из вакансий, убитых
    опечаткой в списке стоп-слов, и оставить его недостижимым значит
    сделать правку конфига бесполезной там, где она нужнее всего.
    `enrich_failed` отделяется полным равенством машинной константы, а
    отказ человека через CLI — тем, что причины у него нет вовсе.
    """
    db_path = str(tmp_path) + "/old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(FIRST_GENERATION_SCHEMA)
    rejections = {
        "отсеяна-опечаткой": "стоп-слово в заголовке: курьер",
        "не-скачалась": "enrich_failed",
        "отказ-человека": None,
    }
    for vacancy_id, reason in rejections.items():
        raw.execute(
            "INSERT INTO vacancy (id, url, title, published_at, status, first_seen_at, "
            "reject_reason) VALUES (?, ?, ?, '2026-07-27T09:00:00+00:00', 'rejected', "
            "'2026-07-20T09:00:00+00:00', ?)",
            (vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", "Курьерский backend", reason),
        )
    raw.commit()
    raw.close()

    repository = SqliteRepository(db_path)
    repository.init_schema()

    assert [key for key, _ in repository.rejected_by_prefilter()] == ["отсеяна-опечаткой"]
    assert read_column(db_path, "reject_code", "не-скачалась") == REJECT_CODE_ENRICH_FAILED
    assert read_column(db_path, "reject_code", "отказ-человека") is None
    # идемпотентно: повторный старт не переписывает уже проставленные коды
    repository.init_schema()
    assert [key for key, _ in repository.rejected_by_prefilter()] == ["отсеяна-опечаткой"]
    assert repository.requeue_prefiltered(["отсеяна-опечаткой"]) == 1
    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["отсеяна-опечаткой"]
    repository.close()


# Возврат из отказа обязан быть атомарным так же, как всё остальное в
# хранилище: процесс на VPS умирает от OOM-kill и `docker stop`, и
# половина возвращённых строк — это база, в которой часть бэклога
# осталась отбракованной, а часть уже в очереди, причём различить их
# нечем. Убиваем процесс ВНУТРИ оператора обработчиком прогресса SQLite:
# `os._exit(1)` не разматывает стек, не закрывает соединение и не
# коммитит ничего — ровно то, что делает SIGKILL.
_KILL_INSIDE_REQUEUE = """
import os
import sys

from hh_search.storage.repository import SqliteRepository

db_path, action, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
repository = SqliteRepository(db_path)
ids = [str(number) for number in range(1, 201)]
steps = 0


def handler() -> int:
    global steps
    steps += 1
    if action == "die" and steps >= budget:
        os._exit(1)
    return 0


repository._connection.set_progress_handler(handler, 1)
repository.requeue_prefiltered(ids)
repository._connection.set_progress_handler(None, 1)
print(steps)
"""

_REQUEUE_ROWS = 200


def _rejected_backlog(db_path: str, journal_mode: str) -> None:
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for number in range(1, _REQUEUE_ROWS + 1):
        repository.add_discovered(make_vacancy(str(number)), "embedded", 9)
        # Одна израсходованная попытка на каждую строку: без неё
        # `enrich_attempts` одинаков до и после возврата, и проверка на
        # разъехавшиеся поля не смотрела бы на счётчик вовсе.
        repository.bump_enrich_attempt(str(number), max_attempts=3)
        repository.mark_rejected(str(number), "стоп-слово: курьер", REJECT_CODE_PREFILTER)
    repository.close()
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA journal_mode={journal_mode}")
    raw.commit()
    raw.close()


def _run_child(
    script: str, db_path: str, action: str, budget: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, script, db_path, action, str(budget)],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_kill_inside_the_requeue_leaves_no_torn_state(tmp_path: Path, journal_mode: str) -> None:
    """Оба режима журнала SQLite: смерть посреди возврата — всё или ничего.

    `executemany` + один `commit()` — это ОДНА транзакция, поэтому либо
    возвращены все строки, либо ни одной, и внутри строки статус, причина,
    код и счётчик попыток не могут разъехаться. Проверяется именно факт
    смерти ВНУТРИ оператора: дочерний процесс печатает число шагов только
    если досчитал до конца, а здесь он обязан умереть молча с кодом 1.
    """
    script = str(tmp_path / "kill.py")
    Path(script).write_text(_KILL_INSIDE_REQUEUE, encoding="utf-8")
    db_path = str(tmp_path / f"{journal_mode}.db")
    _rejected_backlog(db_path, journal_mode)
    backup = str(tmp_path / f"{journal_mode}.bak")

    measured = _run_child(script, db_path, "measure", 0)
    assert measured.returncode == 0, measured.stderr
    steps = int(measured.stdout)
    assert steps > 0
    shutil.copy(db_path, backup)
    _rejected_backlog(db_path, journal_mode)

    killed = _run_child(script, db_path, "die", steps // 2)
    assert killed.returncode == 1 and killed.stdout == "", (
        f"процесс обязан умереть ВНУТРИ оператора: {killed.stdout!r} {killed.stderr!r}"
    )

    raw = sqlite3.connect(db_path)
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    torn = raw.execute(
        "SELECT COUNT(*) FROM vacancy WHERE (status = 'new') IS NOT (reject_code IS NULL) "
        "OR (status = 'new') IS NOT (reject_reason IS NULL) "
        "OR (status = 'new') IS NOT (enrich_attempts = 0)"
    ).fetchone()[0]
    returned = raw.execute("SELECT COUNT(*) FROM vacancy WHERE status = 'new'").fetchone()[0]
    raw.close()
    assert torn == 0, f"{torn} строк с разъехавшимися статусом, причиной, кодом и счётчиком"
    assert returned in (0, _REQUEUE_ROWS), (
        f"возврат порвался на середине: {returned} из {_REQUEUE_ROWS} строк вернулись, "
        "остальные остались отбракованными — различить их в базе больше нечем"
    )
    Path(backup).unlink()


# --- A-M1: порча счётчика попыток не имеет права прятать вакансию ---------


def test_corrupt_enrich_attempts_does_not_hide_the_vacancy(tmp_path: Path) -> None:
    """Единственный вид порчи, который карантин поймать не может в принципе.

    `safe_rows` защищает РАЗБОР строки, а `enrich_attempts` участвует в
    `WHERE`: строка не доходит до разбора. Типы в SQLite динамические, и
    текст в этой колонке делает предикат `< max_attempts` ложным навсегда
    (любое число меньше любого текста). Вакансия без описания при этом
    невидима и `pending_scoring`, и `unreported` — то есть исчезает молча
    и целиком, без единой строки в логе.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET enrich_attempts = 'мусор' WHERE id = '1'")

    repository = SqliteRepository(db_path)
    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    # И счётчик снова становится числом с первой же попытки, поэтому
    # возврат в очередь не превращается в вечный цикл.
    assert repository.bump_enrich_attempt("1", max_attempts=3) == 1
    repository.close()


# --- C1: ручной статус не имеет права создавать невидимое состояние -------


def test_manual_return_to_new_clears_the_exhausted_attempts(tmp_path: Path) -> None:
    """`mark <id> new` — это «попробовать ещё раз», и оно обязано работать.

    Строка с исчерпанными попытками уже терминальна
    (`rejected`/`enrich_failed`). Смена ОДНОГО столбца `status` на `new`
    воссоздавала ровно тот Critical, ради недостижимости которого
    переписывался `bump_enrich_attempt`: `status='new'`,
    `description IS NULL`, `enrich_attempts >= max` — состояние, невидимое
    всем трём выборкам, из которого нет пути назад ничем, кроме сырого
    SQL. Возврат в `new` поэтому обнуляет счётчик и чистит причину тем же
    UPDATE — по образцу `requeue_prefiltered`.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    for _ in range(3):
        repository.bump_enrich_attempt("1", max_attempts=3)
    assert read_column(db_path, "status", "1") == "rejected"

    assert repository.set_status("1", "new") is True

    assert [v.id for v in repository.pending_enrichment(max_attempts=3)] == ["1"]
    assert read_column(db_path, "enrich_attempts", "1") == 0
    assert read_column(db_path, "reject_reason", "1") is None
    assert read_column(db_path, "reject_code", "1") is None
    repository.close()


def test_manual_status_drops_the_machine_reject_code(tmp_path: Path) -> None:
    """M4: решение человека отменяет машинный код, а не сосуществует с ним.

    Иначе `mark X new` на отказе префильтра оставлял `reject_code`
    нетронутым, и следующий `mark X rejected` возвращал вакансию в
    очередь СЛЕДУЮЩИМ же прогоном — вопреки спеке §5.2, где ручной
    `mark <id> rejected` кода не имеет вовсе и не возвращается.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.mark_rejected("1", "совпало стоп-слово: junior", REJECT_CODE_PREFILTER)

    repository.set_status("1", "new")
    repository.set_status("1", "rejected")

    assert read_column(db_path, "reject_code", "1") is None
    assert repository.rejected_by_prefilter() == []
    assert repository.requeue_prefiltered(["1"]) == 0
    assert read_column(db_path, "status", "1") == "rejected"
    repository.close()


def test_manual_status_other_than_new_keeps_the_attempt_counter(tmp_path: Path) -> None:
    """Обнуляется счётчик ровно у `new` — у остальных статусов он ничего не значит,
    а стирать историю попыток без нужды незачем: строка и так терминальна."""
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    for _ in range(2):
        repository.bump_enrich_attempt("1", max_attempts=3)

    repository.set_status("1", "archived")

    assert read_column(db_path, "enrich_attempts", "1") == 2
    repository.close()


def test_manual_reported_sets_the_time_and_stays_findable(tmp_path: Path) -> None:
    """I2: `mark <id> reported` не имеет права прятать вакансию из ОБОИХ путей.

    `status='reported'` уводит строку из `unreported()` (там
    `status='new'`), а пустой `reported_at` — из `reported_since()` (там
    `reported_at >= ?`). Ручная команда отвечала «1 → reported» и кодом 0,
    а вакансия исчезала из отчёта навсегда: ровно тот остаток, который
    коммит c7d4b4d закрыл для `mark X new` и не закрыл здесь.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())

    assert repository.set_status("1", "reported") is True

    assert read_column(db_path, "reported_at", "1") is not None
    found = repository.reported_since(datetime.now(UTC) - timedelta(days=1))
    assert [item.discovered.id for item in found] == ["1"]
    repository.close()


def test_manual_reported_does_not_overwrite_the_original_time(tmp_path: Path) -> None:
    """Время первой отправки — история, и ручная команда её не переписывает.

    Иначе `mark X reported` на уже отправленной вакансии двигал бы её
    вперёд по времени, и `report --since 7d` показывал бы вчерашнюю
    находку сегодняшней.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.mark_reported(["1"])
    first = read_column(db_path, "reported_at", "1")

    repository.set_status("1", "reported")

    assert read_column(db_path, "reported_at", "1") == first
    repository.close()


def test_manual_status_that_is_not_reported_does_not_invent_a_time(tmp_path: Path) -> None:
    """Обратная охрана: `mark X archived` отправкой не является.

    Проставь `reported_at` любому ручному статусу — и `report --since`
    начал бы печатать то, чего никто не отправлял.
    """
    db_path = str(tmp_path / "hh.db")
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)

    repository.set_status("1", "archived")

    assert read_column(db_path, "reported_at", "1") is None
    repository.close()


# --- I2: ни одна выборка не поднимает в память всё подходящее --------------


def _fill_ready(repository: SqliteRepository, count: int) -> None:
    for index in range(count):
        repository.add_discovered(make_vacancy(str(index)), "embedded", 9)
        repository.save_enriched(
            str(index), VacancyDetails(description="Yocto"), make_score(total=float(index))
        )


def test_unreported_never_returns_more_than_the_limit(repo: SqliteRepository) -> None:
    """Потолок и порядок вместе: усечён обязан быть ХВОСТ, а не голова.

    Выборка идёт по убыванию оценки, поэтому отложенное потолком — самое
    низко оценённое, а не случайное. Без этого потолок означал бы, что
    отчёт теряет непредсказуемую часть вакансий.
    """
    _fill_ready(repo, 10)
    ready = repo.unreported(limit=3)
    assert [item.discovered.id for item in ready] == ["9", "8", "7"]


def test_pending_enrichment_limit_caps_requests_to_hh(repo: SqliteRepository) -> None:
    """Длина этой выборки И ЕСТЬ число запросов к hh.ru за прогон."""
    for index in range(10):
        repo.add_discovered(make_vacancy(str(index)), "embedded", 9)
    assert len(repo.pending_enrichment(max_attempts=3, limit=4)) == 4


def test_pending_scoring_is_capped_but_the_stuck_counter_is_not(repo: SqliteRepository) -> None:
    """Счётчик застрявших обязан расти и ЗА потолком выборки.

    Иначе `stuck` в журнале упирался бы ровно в `limit` и переставал
    расти именно там, где беда становится большой, — то есть метрика
    молчала бы про масштаб той самой аварии, ради которой заведена.
    """
    for index in range(10):
        repo.add_discovered(make_vacancy(str(index)), "embedded", 9)
        repo.save_description(str(index), VacancyDetails(description="Yocto"))
    assert len(repo.pending_scoring(limit=4)) == 4
    assert repo.count_pending_scoring() == 10


def test_reported_since_is_capped(repo: SqliteRepository) -> None:
    _fill_ready(repo, 10)
    repo.mark_reported([str(index) for index in range(10)])
    cutoff = datetime.now(UTC) - timedelta(days=7)
    assert len(repo.reported_since(cutoff, limit=3)) == 3


def test_no_selection_is_unbounded_by_default(repo: SqliteRepository) -> None:
    """Умолчание — тоже потолок, а не «сколько найдётся».

    Вызывающий, забывший про лимит (тест, будущий код, ручная отладка),
    обязан получить ограниченную выборку: неограниченная существует
    ровно в том сценарии, где отказ уже случился, и добавляет к нему OOM.
    """
    _fill_ready(repo, DEFAULT_BATCH_LIMIT + 1)
    assert len(repo.unreported()) == DEFAULT_BATCH_LIMIT


def test_pending_titles_covers_the_whole_queue_without_a_limit(repo: SqliteRepository) -> None:
    """Отсев обязан видеть очередь ЦЕЛИКОМ, а не первые `limit` строк.

    Иначе вакансия, вытесненная за границу окна отсева, но попавшая в
    окно обогащения после чужих отказов, ушла бы в сеть, ни разу не
    пройдя единственный барьер перед ней. Читаются при этом ровно две
    колонки — всё, чем живёт решение префильтра.
    """
    for index in range(600):
        repo.add_discovered(make_vacancy(str(index)), "embedded", 9)
    titles = repo.pending_titles(max_attempts=3)
    assert len(titles) == 600
    assert titles[0][1] == "Embedded Linux Engineer"


# --- I3: индексы -----------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Файловая база: индексы и миграция проверяются сырым соединением."""
    return str(tmp_path / "hh.db")


def _indexes(db_path: str) -> set[str]:
    raw = sqlite3.connect(db_path)
    rows = raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
    ).fetchall()
    raw.close()
    return {str(row[0]) for row in rows}


REQUIRED_INDEXES = {
    "idx_vacancy_status",
    "idx_vacancy_reject",
    "idx_vacancy_reported",
    "idx_run_status_finished",
}


def test_fresh_database_gets_every_index(db_path: str) -> None:
    with SqliteRepository(db_path) as repository:
        repository.init_schema()
    assert REQUIRED_INDEXES <= _indexes(db_path)


def test_migration_adds_indexes_to_a_base_without_them(db_path: str) -> None:
    """Механизм миграции обязан уметь добавлять ИНДЕКСЫ, а не только колонки.

    База прошлого поколения индексов не имеет — и не получит их ни от
    `ALTER TABLE`, ни от `CREATE TABLE IF NOT EXISTS`. Проверяется ровно
    это: индексы сносятся сырым SQL, и следующий `init_schema()` их
    возвращает.
    """
    with SqliteRepository(db_path) as repository:
        repository.init_schema()
    raw = sqlite3.connect(db_path)
    for name in REQUIRED_INDEXES:
        raw.execute(f"DROP INDEX {name}")
    raw.commit()
    raw.close()
    assert not (REQUIRED_INDEXES & _indexes(db_path))

    with SqliteRepository(db_path) as repository:
        repository.init_schema()
    assert REQUIRED_INDEXES <= _indexes(db_path)


def test_index_over_a_migrated_column_does_not_break_the_schema(db_path: str) -> None:
    """`idx_vacancy_reject` стоит на колонке, которой у старой базы нет.

    Порядок «сначала schema.sql, потом ALTER» ронял бы весь
    `executescript` на «no such column: reject_code» — и база оставалась
    бы вообще без таблиц `run` и `http_cache`, идущих в файле ниже.
    Здесь воспроизведена именно такая база: колонка удалена, индекс — нет.
    """
    with SqliteRepository(db_path) as repository:
        repository.init_schema()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX idx_vacancy_reject")
    raw.execute("ALTER TABLE vacancy DROP COLUMN reject_code")
    raw.commit()
    raw.close()

    with SqliteRepository(db_path) as repository:
        repository.init_schema()
        repository.add_discovered(make_vacancy("1"), "embedded", 9)
        repository.mark_rejected("1", "стоп-слово", REJECT_CODE_PREFILTER)
        assert repository.rejected_by_prefilter() == [("1", "Embedded Linux Engineer")]
    assert REQUIRED_INDEXES <= _indexes(db_path)


def test_applying_the_schema_twice_changes_nothing(db_path: str) -> None:
    """Идемпотентность: миграция запускается КАЖДЫМ стартом сервиса."""
    with SqliteRepository(db_path) as repository:
        repository.init_schema()
        first = _indexes(db_path)
        repository.init_schema()
        repository.init_schema()
    assert _indexes(db_path) == first


@pytest.mark.parametrize(
    ("sql", "params", "index"),
    [
        (
            "SELECT finished_at FROM run WHERE status IN ('ok', 'partial') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC",
            (),
            "idx_run_status_finished",
        ),
        (
            "UPDATE run SET status = 'interrupted' WHERE status = 'running'",
            (),
            "idx_run_status_finished",
        ),
        (
            "SELECT id FROM vacancy WHERE status = 'rejected' AND reject_code = 'prefilter'",
            (),
            "idx_vacancy_reject",
        ),
        (
            "SELECT id FROM vacancy WHERE status = 'reported' AND reported_at >= '2026-01-01'",
            (),
            "idx_vacancy_reported",
        ),
    ],
)
def test_the_hot_queries_stop_scanning_the_table(
    db_path: str, sql: str, params: tuple[object, ...], index: str
) -> None:
    """План запроса, а не время: время шумит, план — факт.

    `SCAN` в плане означает полный обход таблицы, которая растёт вечно
    (журнал — шесть строк в сутки, отправленные — без границы вовсе).
    """
    with SqliteRepository(db_path) as repository:
        repository.init_schema()
    raw = sqlite3.connect(db_path)
    plan = " ".join(str(row[3]) for row in raw.execute(f"EXPLAIN QUERY PLAN {sql}", params))
    raw.close()
    assert index in plan, plan
    assert "SCAN" not in plan, plan


# --- M10: перестроение таблицы не оставляет за собой удвоенный файл --------


def test_rebuild_returns_the_freed_pages_to_the_file(tmp_path: Path) -> None:
    """`DROP TABLE` в SQLite не уменьшает файл — страницы уходят в freelist.

    Перестроение поэтому удваивало базу навсегда (замер: 1.66 ГБ до,
    3.31 ГБ после на 400 000 строк), и вторая половина не использовалась
    ничем. Проверяется freelist, а не размер файла: он — сам факт, а не
    его следствие, и не шумит от размера страницы.
    """
    db_path = str(tmp_path / "big.db")
    _first_generation_with_rows(db_path, rows=2000)
    with SqliteRepository(db_path) as repository:
        repository.init_schema()

    raw = sqlite3.connect(db_path)
    free = raw.execute("PRAGMA freelist_count").fetchone()[0]
    rows = raw.execute("SELECT COUNT(*) FROM vacancy").fetchone()[0]
    raw.close()
    assert rows == 2000, "перелив обязан сохранить все строки"
    assert free == 0, f"после перестроения в файле осталось {free} свободных страниц"


# --- «регион и формат работы»: множество форматов переживает хранение ------


def test_work_formats_survive_a_round_trip(repo: SqliteRepository) -> None:
    """Множество, а не одно значение: сохранённое обязано читаться обратно
    ровно тем же множеством — через ту же выборку, которой пользуется отчёт."""
    repo.add_discovered(make_vacancy(), "embedded", 9)
    saved = frozenset({WorkFormat.ON_SITE, WorkFormat.REMOTE})
    repo.save_enriched("1", VacancyDetails(description="Yocto", work_formats=saved), make_score())
    assert repo.unreported()[0].details.work_formats == saved


def test_vacancy_without_work_formats_reads_back_as_empty_set(tmp_path: object) -> None:
    """Так лежат 189 вакансий, собранных до этой колонки: NULL в базе,
    `frozenset()` при чтении — не `None` и не исключение."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    assert read_column(db_path, "work_formats", "1") is None
    assert repository.unreported()[0].details.work_formats == frozenset()
    repository.close()


def test_unknown_stored_value_is_ignored_on_read(tmp_path: object) -> None:
    """Порча базы сырым SQL — приём, которым в этом проекте уже находили
    Critical. Незнакомый токен отбрасывается, известный рядом с ним — нет."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    repository.add_discovered(make_vacancy("1"), "embedded", 9)
    repository.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repository.close()

    corrupt(db_path, "UPDATE vacancy SET work_formats = ? WHERE id = ?", "REMOTE,TELEPORT", "1")

    repository = SqliteRepository(db_path)
    assert repository.unreported()[0].details.work_formats == frozenset({WorkFormat.REMOTE})
    repository.close()


# --- R-1: уборка — единственное место, где строки исчезают -----------------


def _backdate_reported_at(repo: SqliteRepository, moment: datetime, *vacancy_ids: str) -> None:
    """`reported_at` в прошлом — публичный API такой даты не ставит.

    `mark_reported` всегда пишет «сейчас» (§5.3 конвейера: время отправки —
    факт истории, а не параметр вызывающего), поэтому состояние «отправлено
    давно» готовится сырым SQL — как и везде в этом файле, где порчу или
    прошлое нельзя получить через публичные методы.
    """
    repo._connection.execute(  # noqa: SLF001 — reported_at в прошлом публичным API не поставить
        f"UPDATE vacancy SET reported_at = ? WHERE id IN ({','.join('?' for _ in vacancy_ids)})",
        (to_utc_iso(moment), *vacancy_ids),
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния


def test_forget_descriptions_only_touches_old_reported_rows(repo: SqliteRepository) -> None:
    """Обнуляются описания отправленных и старых. Всё остальное цело.

    Четыре соседа проверяются вместе, потому что каждый ломается по-своему:
    свежая отправленная теряется из `report --since` раньше срока, строка
    `new` с описанием выпадает из очереди отчёта, строка `new` без описания
    ушла бы в сеть за уже скачанной страницей, а вакансия, отправленная
    давно и возвращённая человеком в `new` командой `mark <id> new`, —
    самый дорогой случай: `set_status` сознательно не трогает `reported_at`
    (время первой отправки — история), и предикат `status = 'reported'`
    здесь единственное, что отличает её от строки «старая». Без него
    уборка обнулила бы уже видимое человеку описание вакансии, которую он
    попросил пересмотреть.
    """
    repo.add_discovered(make_vacancy("старая"), "embedded", 9)
    repo.save_enriched("старая", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["старая"])
    _backdate_reported_at(repo, datetime(2026, 1, 1, tzinfo=UTC), "старая")

    repo.add_discovered(make_vacancy("свежая"), "embedded", 9)
    repo.save_enriched("свежая", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["свежая"])

    repo.add_discovered(make_vacancy("с-описанием"), "embedded", 9)
    repo.save_description("с-описанием", VacancyDetails(description="Yocto"))

    repo.add_discovered(make_vacancy("без-описания"), "embedded", 9)

    repo.add_discovered(make_vacancy("отозванная"), "embedded", 9)
    repo.save_enriched("отозванная", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["отозванная"])
    _backdate_reported_at(repo, datetime(2026, 1, 1, tzinfo=UTC), "отозванная")
    repo.set_status("отозванная", "new")  # человек попросил «попробовать ещё раз»

    freed = repo.forget_descriptions(datetime(2026, 5, 1, tzinfo=UTC))
    assert freed == 1

    # соседи целы: каждый виден ровно той выборке, которой был виден раньше
    recent = repo.reported_since(datetime(2020, 1, 1, tzinfo=UTC))
    assert [item.discovered.id for item in recent] == ["свежая"]
    assert [v.id for v, _ in repo.pending_scoring()] == ["с-описанием"]
    assert [v.id for v in repo.pending_enrichment(max_attempts=3)] == ["без-описания"]
    # status='new' с давним reported_at и целым описанием — видна unreported()
    assert [item.discovered.id for item in repo.unreported()] == ["отозванная"]


def test_forget_descriptions_is_idempotent(repo: SqliteRepository) -> None:
    """Повторный вызов возвращает 0, а не число уже пустых строк.

    Иначе вывод команды врал бы человеку: «убрано 152» на второй прогон
    подряд означало бы, что уборка что-то делает, хотя делать ей нечего.
    """
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2026, 1, 1, tzinfo=UTC), "1")
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)

    assert repo.forget_descriptions(cutoff) == 1
    assert repo.forget_descriptions(cutoff) == 0


def test_a_cleaned_vacancy_is_never_fetched_again(repo: SqliteRepository) -> None:
    """Обнулённое описание не возвращает вакансию в очередь обогащения.

    Самый дорогой из инвариантов уборки: очередь отбирает
    `status='new' AND description IS NULL`, и промах здесь означал бы
    повторный запрос к hh.ru за каждой убранной вакансией плюс повторную
    отправку в Telegram.
    """
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2026, 1, 1, tzinfo=UTC), "1")
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)

    repo.forget_descriptions(cutoff)
    assert repo.pending_enrichment(3, 100) == []


def test_forget_runs_keeps_the_row_that_never_finished(repo: SqliteRepository) -> None:
    """Незакрытая строка журнала переживает уборку.

    `running` без `finished_at` — след убитого процесса, то есть улика
    отказа, ради которой журнал и ведётся. Закроет её
    `close_abandoned_runs()` (переведёт в `interrupted`, не проставив
    `finished_at` — время смерти неизвестно), а удалит уже следующая
    уборка: граница — `COALESCE(finished_at, started_at)`, и `started_at`
    у такой строки есть всегда.
    """
    old_run = repo.start_run()
    repo.finish_run(old_run, "ok", finished_at=datetime(2024, 1, 1, tzinfo=UTC))
    unfinished_run = repo.start_run()  # никогда не закрыт: процесс убит на середине

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_runs_before(cutoff) == 1
    assert repo.forget_runs(cutoff) == 1

    remaining = repo._connection.execute("SELECT id FROM run").fetchall()  # noqa: SLF001
    assert {int(row["id"]) for row in remaining} == {unfinished_run}


def test_forget_runs_deletes_an_old_interrupted_row_without_finished_at(
    repo: SqliteRepository,
) -> None:
    """I2: `interrupted` не получает `finished_at` — и всё равно попадает под срок.

    `close_abandoned_runs()` сознательно не проставляет `finished_at`:
    время смерти неизвестно, и выдумывать его значило бы соврать (см. его
    собственный докстринг). Раньше это делало обещание спеки «365 дней»
    ложным: `forget_runs` сравнивал голый `finished_at`, и строка
    `interrupted` никогда не попадала под `< cutoff` — кладбище росло
    вечно. Граница — `COALESCE(finished_at, started_at)`, и у закрытой
    строки `started_at` есть всегда.
    """
    run_id = repo.start_run()
    repo._connection.execute(  # noqa: SLF001 — «начат давно» публичным API не поставить
        "UPDATE run SET started_at = ? WHERE id = ?",
        (to_utc_iso(datetime(2020, 1, 1, tzinfo=UTC)), run_id),
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния
    assert repo.close_abandoned_runs() == 1

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_runs_before(cutoff) == 1
    assert repo.forget_runs(cutoff) == 1


def test_forget_runs_never_deletes_a_running_row(repo: SqliteRepository) -> None:
    """I2: `running` не удаляется никогда, сколько бы ни было её `started_at`.

    Живой прогон обязан пережить уборку, даже если формально «стар» по
    дате старта: граница по `COALESCE(finished_at, started_at)` без
    отдельной защиты `status != 'running'` удалила бы идущий прогон,
    длящийся дольше срока хранения журнала.
    """
    run_id = repo.start_run()
    repo._connection.execute(  # noqa: SLF001 — «начат давно» публичным API не поставить
        "UPDATE run SET started_at = ? WHERE id = ?",
        (to_utc_iso(datetime(2020, 1, 1, tzinfo=UTC)), run_id),
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_runs_before(cutoff) == 0
    assert repo.forget_runs(cutoff) == 0

    remaining = repo._connection.execute("SELECT id FROM run").fetchall()  # noqa: SLF001
    assert {int(row["id"]) for row in remaining} == {run_id}


def test_descriptions_before_counts_bytes_not_characters(repo: SqliteRepository) -> None:
    """Байты, а не символы: описания кириллические, разница вдвое.

    Число уезжает человеку в вывод команды как «освободится N МБ», и
    ошибка вдвое сделала бы его бесполезным.
    """
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_description("1", VacancyDetails(description="ЖЖЖ"))
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2026, 1, 1, tzinfo=UTC), "1")

    cutoff = datetime(2026, 5, 1, tzinfo=UTC)
    rows, size = repo.descriptions_before(cutoff)
    assert (rows, size) == (1, 6)


# --- I3: любое НЕ-ISO значение в колонке границы не считается «старым» -----
#
# В SQLite типы сортируются `NULL < INTEGER/REAL < TEXT < BLOB`, поэтому
# любое число в текстовой колонке меньше любой ISO-строки независимо от
# значения, а укороченный или пустой текст лексикографически меньше полной
# даты («0», «2026», «» — все меньше «2026-05-...»). Без защиты `reported_at
# < ?` / `finished_at < ?` считает такую строку «старше границы» и обнуляет
# или удаляет её молча — в необратимом месте хранилища.
CORRUPT_BOUNDARY_VALUES: tuple[object, ...] = (
    b"\xff\xfe not utf8",
    "не-дата",
    "2026-13-45T99:99",
    "",
    "0",
    "2026",
    "  ",
    0,
    17,
)


@pytest.mark.parametrize("corrupt_value", CORRUPT_BOUNDARY_VALUES)
def test_forget_descriptions_ignores_corrupt_reported_at(
    repo: SqliteRepository, corrupt_value: object
) -> None:
    """I3: порченый `reported_at` не считается «старым» ни в каком виде.

    Ни одна из девяти форм порчи не имеет права дать `forget_descriptions`
    обнулить описание: строка либо переживёт следующую уборку с валидной
    датой, либо останется уликой для человека — но не исчезнет молча.
    """
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    repo._connection.execute(  # noqa: SLF001 — порча ровно той формы, что бывает на диске
        "UPDATE vacancy SET reported_at = ? WHERE id = ?", (corrupt_value, "1")
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния

    cutoff = datetime(2026, 5, 1, tzinfo=UTC)
    assert repo.descriptions_before(cutoff) == (0, 0)
    assert repo.forget_descriptions(cutoff) == 0
    remaining = repo._connection.execute(  # noqa: SLF001 — проверка, что описание цело
        "SELECT description FROM vacancy WHERE id = ?", ("1",)
    ).fetchone()
    assert remaining["description"] == "Yocto"


@pytest.mark.parametrize("corrupt_value", CORRUPT_BOUNDARY_VALUES)
def test_forget_runs_ignores_corrupt_finished_at(
    repo: SqliteRepository, corrupt_value: object
) -> None:
    """I3: порченый `finished_at` не считается «старым» ни в каком виде.

    Для `run` цена промаха выше, чем для `vacancy`: строка не обнуляется, а
    удаляется, и испорченный `finished_at` сам по себе улика — терять её
    вместе со строкой значит стирать след того самого отказа, ради которого
    журнал ведётся.
    """
    run_id = repo.start_run()
    repo.finish_run(run_id, "ok", finished_at=datetime(2020, 1, 1, tzinfo=UTC))
    repo._connection.execute(  # noqa: SLF001 — порча ровно той формы, что бывает на диске
        "UPDATE run SET finished_at = ? WHERE id = ?", (corrupt_value, run_id)
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.count_runs_before(cutoff) == 0
    assert repo.forget_runs(cutoff) == 0

    remaining = repo._connection.execute("SELECT id FROM run").fetchall()  # noqa: SLF001
    assert {int(row["id"]) for row in remaining} == {run_id}


def test_vacuum_shrinks_the_file_after_descriptions_are_cleared(tmp_path: Path) -> None:
    """Без VACUUM файл не ужимается вовсе — и уборка выглядит сломанной.

    Проверяется на ФАЙЛОВОЙ базе: у `:memory:` размера нет, и сторож был
    бы зелен вакуумно.
    """
    path = tmp_path / "hh.db"
    disk = SqliteRepository(path)
    disk.init_schema()

    ids = [str(index) for index in range(200)]
    for vacancy_id in ids:
        disk.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        disk.save_description(vacancy_id, VacancyDetails(description="x" * 4096))
        disk.mark_reported([vacancy_id])
    _backdate_reported_at(disk, datetime(2026, 1, 1, tzinfo=UTC), *ids)
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)

    before = path.stat().st_size
    disk.forget_descriptions(cutoff)
    after_update = path.stat().st_size
    disk.vacuum()
    after_vacuum = path.stat().st_size
    assert after_update >= before, "UPDATE ... = NULL сам по себе файл не ужимает"
    assert after_vacuum < before
    disk.close()


def _as_housekeeper(repo: Housekeeper) -> Housekeeper:
    """Совместимость с протоколом уборки, зафиксированная для `mypy --strict`."""
    return repo


def test_sqlite_repository_satisfies_the_housekeeper_protocol(repo: SqliteRepository) -> None:
    """Доказательство — пара «тест зелёный» и «файл проходит mypy --strict».

    Рантайм здесь не доказывает ничего: протоколы структурные, и
    несовпадение сигнатуры видит только проверка типов.
    """
    assert _as_housekeeper(repo) is repo
