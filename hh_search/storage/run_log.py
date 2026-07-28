"""Журнал прогонов и HTTP-кэш условных запросов.

Вынесены из repository.py отдельным модулем ради размера файла, но
работают поверх того же `sqlite3.Connection`, что и `SqliteRepository` —
соединением не владеют и не закрывают его. Инвариант «весь SQL живёт в
слое storage» сохранён: обе таблицы (`run`, `http_cache`) обслуживаются
только здесь.
"""

import logging
import sqlite3
from datetime import UTC, datetime

from hh_search.storage.mappers import decode_text
from hh_search.storage.time_utils import now_iso, parse_utc, to_utc_iso

ALLOWED_RUN_COUNTERS = {
    "discovered",
    "new_count",
    "rejected",
    "enriched",
    "reported",
    "rescored",
    "stuck",
    "error",
}

logger = logging.getLogger(__name__)


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
        *,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        # `finished_at` только по имени: он стоял третьим позиционным
        # параметром рядом с `**counters`, поэтому значение счётчика,
        # переданное позиционно, молча уезжало в дату завершения, а имя
        # счётчика при этом отбрасывалось белым списком ниже. Ошибка была
        # тихой с двух сторон: и дата неверная, и счётчик потерян.
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
        """Время последнего успешного прогона; от него зависит healthcheck.

        Одна испорченная строка журнала не имеет права заклинить сервис
        навсегда, поэтому здесь тот же приём, что и в выборках вакансий:
        `CAST(... AS BLOB)` (иначе битый UTF-8 роняет весь курсор ещё на
        fetch) плюс разбор по одной строке. Нечитаемая дата пропускается
        с записью в лог, ответом становится ближайшая читаемая — сервис
        в худшем случае считает себя протухшим, но остаётся живым.
        LIMIT 1 убран намеренно: с ним битая верхняя строка означала бы
        «успешных прогонов нет вовсе».
        """
        cursor = self._connection.execute(
            "SELECT CAST(finished_at AS BLOB) AS finished_at FROM run "
            "WHERE status IN ('ok', 'partial') AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC"
        )
        for row in cursor:
            raw = row["finished_at"]
            try:
                return parse_utc(decode_text(raw))
            except ValueError:
                logger.error(
                    "журнал прогонов: finished_at = %r не разбирается как дата, строка пропущена",
                    raw,
                    exc_info=True,
                )
        return None

    # --- conditional requests ------------------------------------------

    def cache_headers(self, url: str) -> dict[str, str]:
        """Валидаторы условного запроса; значения гарантированно `str`.

        Без `CAST(... AS BLOB)` битый UTF-8 в кэше ронял бы fetch, а без
        разбора здесь `bytes` уезжали бы прямо в HTTP-заголовок. Нечитаемый
        валидатор просто не отправляется: худший исход — лишний полный
        ответ вместо 304, а не сломанный запрос.
        """
        row = self._connection.execute(
            "SELECT CAST(etag AS BLOB) AS etag, "
            "CAST(last_modified AS BLOB) AS last_modified FROM http_cache WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        for header, column in (("If-None-Match", "etag"), ("If-Modified-Since", "last_modified")):
            value = _safe_text(row[column], column)
            if value:
                headers[header] = value
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


def _safe_text(value: bytes | str | None, column: str) -> str | None:
    if value is None:
        return None
    try:
        return decode_text(value)
    except ValueError:
        logger.error(
            "http-кэш: колонка %s не декодируется (%r), валидатор не отправлен", column, value
        )
        return None
