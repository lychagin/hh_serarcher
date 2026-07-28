"""Догоняющая миграция уже существующей базы до актуальной схемы.

`CREATE TABLE IF NOT EXISTS` на базе, созданной предыдущей версией
сервиса, не делает ровно ничего: новые колонки не появляются, и первая же
выборка падает с `no such column`. Путь к базе персистентный (том Docker
на VPS), схема применяется при старте — то есть без миграции обновление
сервиса означает мёртвый сервис.

Механизм выбран самый скучный из возможных: спросить у SQLite, какие
колонки есть (`PRAGMA table_info`), и добавить недостающие
(`ALTER TABLE ... ADD COLUMN`). Он идемпотентен, не требует собственного
состояния и одинаково работает с базой любого прошлого поколения — в
отличие от `PRAGMA user_version`, который во всех уже выпущенных базах
равен нулю независимо от их реального возраста.

Одно изменение через ADD COLUMN невыразимо: снятие NOT NULL с колонки
`published_at`. SQLite не умеет ослаблять ограничение существующей
колонки, а после переезда discovery на листинг дата публикации на момент
вставки строки неизвестна. Для этого случая ниже есть перестроение
таблицы — тоже без собственного знания о схеме: старая таблица
отодвигается в сторону, актуальную создаёт сам `schema.sql`, строки
переливаются по пересечению колонок.

Единственный необратимый шаг всего модуля — именно это перестроение, и
атомарным его сделать нельзя: `connection.executescript()` неявно
коммитит отложенную транзакцию, поэтому состояние «старая таблица
отодвинута, новая пустая создана, строки ещё не перелиты» ДОЛГОВЕЧНО.
Процесс, умерший в этом окне (OOM-kill на VPS, `docker stop`, SIGKILL),
оставляет базу с пустой `vacancy` и полной отодвинутой таблицей — и
`PRAGMA integrity_check` при этом честно говорит `ok`. Поэтому
`apply_schema` начинается не с попытки начать миграцию, а с проверки, не
надо ли ДОИГРАТЬ прерванную: отодвинутая таблица на диске — это не
улика, а незавершённая работа.
"""

import sqlite3

# (таблица, колонка, определение) — только те колонки, что появлялись
# после первой версии схемы. Порядок не важен: каждая строка
# применяется независимо и только если колонки ещё нет. Имена берутся
# из этой константы и никогда из внешних данных — f-строка ниже
# безопасна по построению.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("vacancy", "primary_query", "TEXT NOT NULL DEFAULT ''"),
    ("vacancy", "corrupt_payload", "BLOB"),
    ("vacancy", "valid_through", "TEXT"),
    ("vacancy_query", "weight", "INTEGER NOT NULL DEFAULT 0"),
    ("run", "rescored", "INTEGER DEFAULT 0"),
    ("run", "stuck", "INTEGER DEFAULT 0"),
    ("vacancy", "reject_code", "TEXT"),
    ("run", "requeued", "INTEGER DEFAULT 0"),
    ("run", "stalled", "INTEGER DEFAULT 0"),
    ("run", "corrupted", "INTEGER DEFAULT 0"),
)

# У отказов, накопленных базой прошлого поколения, машинного кода нет:
# колонки не существовало. Решение сознательное — считать их отказами
# ПРЕФИЛЬТРА, то есть обратимыми. Причина в том, ради чего обратимость
# вводится: накопленный бэклог как раз и состоит из вакансий, убитых
# опечаткой в списке стоп-слов, и оставить его недостижимым значит
# сделать правку конфига наполовину бесполезной именно там, где она
# нужнее всего.
#
# Разбора человекочитаемого текста здесь нет: `enrich_failed` — машинная
# константа, которую пишет `bump_enrich_attempt` целиком, и сравнение
# идёт на полное равенство, а не по префиксу. Отказ, поставленный
# человеком через CLI (`mark <id> rejected`), отличается тем, что
# причины у него нет вовсе, и код ему не проставляется: возвращать чужое
# решение переоценкой заголовка нельзя.
#
# Идемпотентно и переживает прерывание: WHERE отбирает только строки без
# кода, повторный прогон их не находит, а сам UPDATE атомарен.
_BACKFILL_REJECT_CODE = """
UPDATE vacancy SET reject_code = CASE
        WHEN reject_reason = 'enrich_failed' THEN 'enrich_failed'
        ELSE 'prefilter'
    END
WHERE status = 'rejected' AND reject_code IS NULL AND reject_reason IS NOT NULL
"""

# ALTER TABLE ставит всем существующим строкам DEFAULT, то есть у всей
# мигрированной базы primary_query = ''. А `found_by_query` в отчёте
# берётся именно оттуда — для старых вакансий отчёт молча показывал бы
# пустой запрос. Реальные запросы при этом никуда не делись: они лежат в
# vacancy_query. Победитель выбирается детерминированно (самый тяжёлый,
# при равенстве — лексикографически первый), иначе миграция на разных
# репликах дала бы разный результат.
_BACKFILL_PRIMARY_QUERY = """
UPDATE vacancy SET primary_query = (
    SELECT vq.query FROM vacancy_query vq WHERE vq.vacancy_id = vacancy.id
    ORDER BY vq.weight DESC, vq.query LIMIT 1
)
WHERE primary_query = ''
  AND EXISTS (SELECT 1 FROM vacancy_query vq WHERE vq.vacancy_id = vacancy.id)
"""

# Имя отодвинутой таблицы. В обычном прогоне живёт внутри одного вызова
# `apply_schema`, но переживает смерть процесса — и тогда становится
# единственным свидетельством того, что перестроение начато и не
# закончено. Имя поэтому фиксированное, а не случайное: следующий старт
# обязан его узнать.
_LEGACY_VACANCY = "vacancy_before_nullable_published_at"


def apply_schema(connection: sqlite3.Connection, schema_sql: str) -> None:
    """Создать схему и догнать до неё уже существующую базу.

    Порядок значим: устаревшую таблицу надо отодвинуть ДО того, как
    `schema.sql` создаст актуальную, иначе `CREATE TABLE IF NOT EXISTS`
    увидит старую и не сделает ничего.

    Начинается всё с `_resume_or_detach`, а не с попытки отодвинуть: если
    отодвинутая таблица уже лежит на диске, значит прошлый процесс умер
    посреди перестроения, и первым делом надо доиграть ЕГО работу.
    """
    detached = _resume_or_detach(connection)
    connection.executescript(schema_sql)
    if detached:
        _refill_from_legacy(connection)
    for table, column, definition in ADDED_COLUMNS:
        if column not in _columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Идемпотентно: заполняют только пустые значения, повторный прогон не
    # находит их и не делает ничего. Выполняются после ALTER — на этот
    # момент все участвующие колонки заведомо существуют.
    connection.execute(_BACKFILL_PRIMARY_QUERY)
    connection.execute(_BACKFILL_REJECT_CODE)
    connection.commit()


def _resume_or_detach(connection: sqlite3.Connection) -> bool:
    """Доиграть прерванное перестроение или начать его. True — надо перелить.

    Оставшаяся на диске отодвинутая таблица значит ровно одно: прошлый
    процесс успел отодвинуть, но не успел перелить. Проверять при этом
    `published_at` у новой `vacancy` бессмысленно — она уже nullable, и
    именно поэтому прежняя версия возвращала здесь False и не переливала
    НИКОГДА: строки навсегда оставались в отодвинутой таблице, `vacancy`
    была пуста, а `apply_schema` возвращался без исключения.
    """
    if _LEGACY_VACANCY in _tables(connection):
        _suspend_foreign_keys(connection)
        return True
    return _detach_vacancy_with_not_null_published_at(connection)


def _suspend_foreign_keys(connection: sqlite3.Connection) -> None:
    """Выключить проверку внешних ключей на время перестроения.

    `commit()` обязателен: внутри транзакции PRAGMA молча игнорируется, и
    перелив шёл бы с включённой проверкой.
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")


def _detach_vacancy_with_not_null_published_at(connection: sqlite3.Connection) -> bool:
    """Отодвинуть таблицу `vacancy`, если у неё published_at ещё NOT NULL.

    `legacy_alter_table=ON` обязателен: без него SQLite переписал бы
    `REFERENCES vacancy(id)` в таблице vacancy_query на новое имя, и
    внешний ключ после миграции указывал бы на временную таблицу,
    которой через три оператора не станет.

    Индексы переезжают вместе с таблицей и потому сносятся явно: имя
    индекса в SQLite глобально, поэтому `CREATE INDEX IF NOT EXISTS` из
    schema.sql увидел бы уцелевший старый индекс, не создал бы новый — и
    после удаления отодвинутой таблицы актуальная осталась бы без индекса.
    """
    if "vacancy" not in _tables(connection):
        return False
    if not _is_not_null(connection, "vacancy", "published_at"):
        return False
    _suspend_foreign_keys(connection)
    connection.execute("PRAGMA legacy_alter_table=ON")
    for index in _indexes_of(connection, "vacancy"):
        connection.execute(f"DROP INDEX {index}")
    connection.execute(f"ALTER TABLE vacancy RENAME TO {_LEGACY_VACANCY}")
    connection.execute("PRAGMA legacy_alter_table=OFF")
    return True


def _refill_from_legacy(connection: sqlite3.Connection) -> None:
    """Перелить строки в свежесозданную `vacancy` и убрать отодвинутую.

    Колонки берутся по пересечению: собственного знания о схеме здесь
    нет, поэтому список нечему разойтись с `schema.sql`. Колонки, которых
    в старой базе не было, останутся при своих DEFAULT — их дозаполняет
    обычный проход ADD COLUMN и бэкфилл.

    `INSERT OR IGNORE`, а не `INSERT`: перелив мог уже начаться и умереть
    на середине, и тогда часть строк в `vacancy` есть. Простой INSERT
    упал бы на первой из них с IntegrityError, то есть прерванная
    миграция стала бы невосстановимой ещё и громко. Конфликт возможен
    только по первичному ключу, а строка с этим ключом либо перелита
    отсюда же (то же значение), либо записана уже после миграции (значение
    новее) — в обоих случаях права та, что в `vacancy`.
    """
    shared = sorted(_columns(connection, _LEGACY_VACANCY) & _columns(connection, "vacancy"))
    columns = ", ".join(shared)  # имена пришли из PRAGMA, не из внешних данных
    connection.execute(
        f"INSERT OR IGNORE INTO vacancy ({columns}) SELECT {columns} FROM {_LEGACY_VACANCY}"
    )
    connection.execute(f"DROP TABLE {_LEGACY_VACANCY}")
    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:  # pragma: no cover - перелив идёт по первичному ключу один в один
        raise RuntimeError(f"миграция vacancy порвала внешние ключи: {broken!r}")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def _indexes_of(connection: sqlite3.Connection, table: str) -> list[str]:
    """Только явно созданные индексы: у автоиндексов первичного ключа
    `sql IS NULL`, и удалить их нельзя (да и не нужно — они уедут с таблицей)."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _is_not_null(connection: sqlite3.Connection, table: str, column: str) -> bool:
    for row in connection.execute(f"PRAGMA table_info({table})").fetchall():
        if str(row[1]) == column:
            return bool(row[3])
    return False


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}
