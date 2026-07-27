CREATE TABLE IF NOT EXISTS vacancy (
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
    primary_query   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    first_seen_at   TEXT NOT NULL,
    reported_at     TEXT,
    corrupt_count   INTEGER NOT NULL DEFAULT 0,
    corrupt_payload BLOB
);

CREATE INDEX IF NOT EXISTS idx_vacancy_status ON vacancy(status);

-- primary_query всегда переписывается тем же UPDATE, что и cluster/
-- cluster_weight (см. repository.py), поэтому в отчёте found_by_query
-- гарантированно совпадает с запросом, определившим кластер. Таблица
-- ниже хранит ПОЛНЫЙ список запросов, которыми была найдена вакансия
-- (для будущей аналитики), но репозиторий больше не выбирает
-- "победителя" из неё через недетерминированный подзапрос.
CREATE TABLE IF NOT EXISTS vacancy_query (
    vacancy_id TEXT NOT NULL REFERENCES vacancy(id),
    query      TEXT NOT NULL,
    weight     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vacancy_id, query)
);

CREATE TABLE IF NOT EXISTS run (
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

CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
