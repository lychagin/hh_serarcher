CREATE TABLE IF NOT EXISTS vacancy (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    area            TEXT,
    -- Множество форматов работы (REMOTE/HYBRID/ON_SITE/FIELD_WORK),
    -- отсортированный список через запятую в одной колонке, а не
    -- отдельная таблица: значений всего четыре, выборок по ним нет (штраф
    -- в скоринге читает всю строку и проверяет вхождение в Python), и
    -- нормализация дала бы join ради ничего. Сортировка перед склейкой —
    -- чтобы одно и то же множество всегда давало один и тот же текст
    -- (иначе круговой тест хранения и score_detail стали бы недетерминированными).
    -- NULL — вакансия ещё не обогащена (либо обогащена до этой колонки:
    -- 189 живых записей) или страница не содержала блока формата.
    work_formats    TEXT,
    salary_raw      TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    salary_currency TEXT,
    -- NULL до обогащения: discovery идёт по листингу /vacancies/{slug},
    -- который отдаёт только id, url и заголовок. Дата публикации, как и
    -- company/area/salary, приходит со страницы вакансии. Всё, что
    -- сортировалось по этой колонке, обязано падать на first_seen_at.
    published_at    TEXT,
    -- Дата, после которой вакансия неактуальна (validThrough из JSON-LD).
    valid_through   TEXT,
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
    -- Машинный код причины отказа: 'prefilter' обратим, 'enrich_failed'
    -- нет. Отдельная колонка, а не префикс в reject_reason: текст
    -- причины принадлежит человеку и меняется, и разъехавшийся префикс
    -- молча изменил бы множество возвращаемых вакансий. NULL — отказ,
    -- поставленный человеком через CLI: он не возвращается.
    reject_code     TEXT,
    first_seen_at   TEXT NOT NULL,
    reported_at     TEXT,
    -- Улики: исходное значение score_detail, которое не удалось
    -- прочитать. Пишется через COALESCE (первая порча не затирается
    -- последующими) и BLOB, а не TEXT, — payload может сам быть
    -- невалидным UTF-8. Счётчика попыток рядом нет сознательно: он
    -- ограничивал сетевой цикл переобогащения, а порча оценки больше
    -- не приводит к обращениям в сеть (см. quarantine.py).
    corrupt_payload BLOB,
    -- Вектор описания: 1024 значения float32 с ЯВНЫМ порядком байт
    -- (llm/semantic.py), 4 КБ на вакансию. NULL — вакансия ещё не
    -- эмбеддилась, эмбеддинг выключен конфигом или описание снято
    -- уборкой (вектор уходит вместе с ним: производная не имеет права
    -- пережить исходник).
    embedding       BLOB,
    -- Имя модели, ОТДАВШЕЙ вектор выше. Лежит рядом не для истории:
    -- вектор bge-m3 и вектор любой другой модели живут в разных
    -- пространствах, и косинус между ними посчитается, выйдет
    -- правдоподобным и не будет значить ничего. Выборки сравнивают это
    -- поле с текущим `llm.embed_model`, поэтому правка конфига
    -- обесценивает корпус САМА, без ручной чистки базы.
    embedding_model TEXT,
    -- Факты, выписанные из описания локальной моделью (VacancyFacts как
    -- JSON): стек, требуемый опыт, грейд. НЕ суждение о пригодности —
    -- замер 2026-08-26 развёл роли по способностям модели.
    llm_facts       TEXT,
    -- Имя модели, извлёкшей факты выше. Та же роль, что у
    -- embedding_model: другая модель извлекает иначе, и отчёт, смешавший
    -- два поколения, выглядел бы согласованным, не будучи им.
    llm_facts_model TEXT
);

-- Три состояния строки со status = 'new' различаются только парой
-- (description, score_detail) и разбираются тремя непересекающимися
-- выборками репозитория; description после первой записи не обнуляет
-- никто, поэтому страница вакансии скачивается не более одного раза.

CREATE INDEX IF NOT EXISTS idx_vacancy_status ON vacancy(status);

-- Возврат из отказа префильтра (`rejected_by_prefilter`) перебирал ВСЕ
-- отказанные строки, чтобы отобрать из них обратимые, и делал это каждый
-- прогон. Колонки ровно те и в том порядке, в каком стоят в предикате.
-- Колонка `reject_code` появляется у мигрирующей базы только на шаге
-- ALTER TABLE, поэтому он идёт ДО применения этого файла (см.
-- migrations.py): иначе CREATE INDEX уронил бы весь executescript на
-- «no such column», не создав и остальных таблиц.
CREATE INDEX IF NOT EXISTS idx_vacancy_reject ON vacancy(status, reject_code);

-- `reported_since` — единственный способ человека вернуть историю, и он
-- шёл полным перебором отправленных, которые копятся вечно. Дата второй
-- колонкой: по ней идёт диапазонное сравнение, и в составном индексе
-- такое поле обязано стоять последним.
CREATE INDEX IF NOT EXISTS idx_vacancy_reported ON vacancy(status, reported_at);

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
    -- Наблюдаемость локального цикла пересчёта: rescored — сколько
    -- оценок пересчитано за прогон, stuck — сколько вакансий осталось
    -- ждать пересчёта после него. Ненулевой stuck прогон за прогоном
    -- означает «очередь пересчёта не сходится» — это метрика прогона,
    -- а не состояние на вакансии, поэтому живёт здесь.
    rescored    INTEGER DEFAULT 0,
    stuck       INTEGER DEFAULT 0,
    -- Сколько вакансий вернулось из отказа префильтра за прогон. Правка
    -- списка стоп-слов обязана быть видимой: возврат бэклога — это
    -- работа прогона, а не тихое событие.
    requeued    INTEGER DEFAULT 0,
    -- Сколько вакансий выведено из очереди обогащения снижением
    -- enrich.max_attempts: строка остаётся `new` с пустым описанием и не
    -- видна НИ ОДНОЙ из трёх выборок, а `stuck` её не считает (там
    -- description IS NOT NULL). Без этого счётчика правка конфига вниз
    -- выглядела бы как прогон, которому просто нечего делать.
    stalled     INTEGER DEFAULT 0,
    -- Сколько вакансий ушло в карантин терминально (status='corrupt') за
    -- прогон. Потеря навсегда, и до этого счётчика она не отражалась ни
    -- статусом, ни причиной: изоляция порчи работала, наблюдаемость нет.
    corrupted   INTEGER DEFAULT 0,
    error       TEXT
);

-- `last_successful_run()` — это и есть healthcheck, а `close_abandoned_runs()`
-- вызывается каждым прогоном; обе обходили таблицу целиком, а она растёт
-- шесть строк в сутки и не чистится ничем. Одна пара колонок обслуживает
-- обе: статус равенством, дата — сортировкой сразу за ним, поэтому
-- `ORDER BY finished_at DESC` берётся из индекса, без отдельной сортировки.
CREATE INDEX IF NOT EXISTS idx_run_status_finished ON run(status, finished_at);

CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
