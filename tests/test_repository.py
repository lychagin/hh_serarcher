import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from hh_search.domain.models import DiscoveredVacancy, Salary, ScoreBreakdown, VacancyDetails
from hh_search.storage.quarantine import STATUS_CORRUPT
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
        "UPDATE vacancy SET score_detail = CAST(x'FFFEFA696E76616C6964' AS TEXT) "
        "WHERE id = ?",
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
    У вакансии "3" вдобавок исчерпаны попытки докачки (3 из 3) — на
    локальный пересчёт это не влияет, потому что сеть не задействуется."""
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="Yocto"), make_score())
    for _ in range(3):
        repository.bump_enrich_attempt("3")
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
    # SQLite побайтово, до какого-либо декодирования
    assert repository.known_ids(["1", "2", "3", "4"]) == {"1", "3"}
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
    repository.mark_rejected("4", "стоп-слово")
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
