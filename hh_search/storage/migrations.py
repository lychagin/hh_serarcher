"""Накатывание недостающих колонок на уже существующую базу.

`init_schema()` — это `CREATE TABLE IF NOT EXISTS`: на базе, созданной
предыдущей версией сервиса, он не делает ровно ничего, новые колонки не
появляются, и первая же выборка падает с `no such column`. Путь к базе
персистентный (том Docker на VPS), `init_schema()` вызывается при старте,
то есть без миграции обновление сервиса означает мёртвый сервис.

Механизм выбран самый скучный из возможных: спросить у SQLite, какие
колонки есть (`PRAGMA table_info`), и добавить недостающие
(`ALTER TABLE ... ADD COLUMN`). Он идемпотентен, не требует собственного
состояния и одинаково работает с базой любого прошлого поколения — в
отличие от `PRAGMA user_version`, который во всех уже выпущенных базах
равен нулю независимо от их реального возраста.
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
    ("vacancy_query", "weight", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate(connection: sqlite3.Connection) -> None:
    for table, column, definition in ADDED_COLUMNS:
        if column not in _columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}
