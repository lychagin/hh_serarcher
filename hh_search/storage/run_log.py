"""Журнал прогонов и HTTP-кэш условных запросов.

Вынесены из repository.py отдельным модулем ради размера файла, но
работают поверх того же `sqlite3.Connection`, что и `SqliteRepository` —
соединением не владеют и не закрывают его. Инвариант «весь SQL живёт в
слое storage» сохранён: обе таблицы (`run`, `http_cache`) обслуживаются
только здесь.
"""

import sqlite3
from datetime import UTC, datetime

from hh_search.storage.time_utils import now_iso, parse_utc, to_utc_iso

ALLOWED_RUN_COUNTERS = {"discovered", "new_count", "rejected", "enriched", "reported", "error"}


class RunLog:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def start_run(self) -> int:
        cursor = self._connection.execute(
            "INSERT INTO run (started_at, status) VALUES (?, 'running')", (now_iso(),)
        )
        self._connection.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        status: str,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        # Белый список: имена полей никогда не приходят от внешнего
        # пользователя, но фильтрация всё равно жёсткая — только
        # известные столбцы таблицы `run` попадают в текст запроса,
        # значения всегда идут параметрами.
        fields = {name: value for name, value in counters.items() if name in ALLOWED_RUN_COUNTERS}
        assignments = ", ".join(f"{name} = ?" for name in fields)
        prefix = f"{assignments}, " if assignments else ""
        moment = to_utc_iso(finished_at or datetime.now(UTC))
        self._connection.execute(
            f"UPDATE run SET {prefix}status = ?, finished_at = ? WHERE id = ?",
            (*fields.values(), status, moment, run_id),
        )
        self._connection.commit()

    def last_successful_run(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT finished_at FROM run WHERE status IN ('ok', 'partial') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return parse_utc(row["finished_at"]) if row else None

    # --- conditional requests ------------------------------------------

    def cache_headers(self, url: str) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        # Пишем честно, даже если оба валидатора пусты: иначе протухший
        # etag из предыдущего ответа невозможно стереть повторным вызовом,
        # и условный запрос будет стабильно получать 304 навсегда.
        self._connection.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, "
            "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
            (url, etag, last_modified, now_iso()),
        )
        self._connection.commit()

    def reset_cache(self, url: str) -> None:
        """Явный сброс кэша — аварийный выход, если валидатор протух."""
        self._connection.execute("DELETE FROM http_cache WHERE url = ?", (url,))
        self._connection.commit()
