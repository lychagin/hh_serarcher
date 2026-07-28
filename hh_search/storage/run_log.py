"""Журнал прогонов и HTTP-кэш условных запросов.

Вынесены из repository.py отдельным модулем ради размера файла, но
работают поверх того же `sqlite3.Connection`, что и `SqliteRepository` —
соединением не владеют и не закрывают его. Инвариант «весь SQL живёт в
слое storage» сохранён: обе таблицы (`run`, `http_cache`) обслуживаются
только здесь.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hh_search.storage.mappers import decode_text
from hh_search.storage.time_utils import now_iso, parse_utc, to_utc_iso

# Насколько дата завершения прогона может опережать «сейчас», оставаясь
# правдоподобной. Пишет её наш же процесс, поэтому речь только о дрожании
# часов между запуском и чтением, а не о часовых поясах: `finished_at`
# всегда UTC. Всё, что дальше, — след скачка часов, и доверять ему нельзя
# (см. `last_successful_run`).
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

ALLOWED_RUN_COUNTERS = {
    "discovered",
    "new_count",
    "rejected",
    "enriched",
    "reported",
    "rescored",
    "stuck",
    "requeued",
    "stalled",
    "corrupted",
    "error",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    """Строка журнала в том виде, в каком её показывают человеку.

    Все поля — строки, и это не небрежность: значения приходят из базы,
    которая типизирована динамически и может быть испорчена, а
    единственный потребитель печатает их в одну строку. Разбирать число,
    чтобы тут же превратить его обратно в текст, значило бы завести здесь
    отказ там, где отказывать нечему.
    """

    status: str
    finished_at: str | None
    error: str | None
    discovered: str
    enriched: str
    reported: str

    def describe(self) -> str:
        finished = self.finished_at or "не закрыт"
        reason = f", причина: {self.error}" if self.error else ""
        return (
            f"последний прогон: {self.status} ({finished}), найдено {self.discovered}, "
            f"обогащено {self.enriched}, отправлено {self.reported}{reason}"
        )


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

    def close_abandoned_runs(self) -> int:
        """Закрыть строки `running`, оставшиеся от умерших процессов.

        SIGKILL, OOM-kill и `docker kill` не дают конвейеру закрыть строку
        журнала, и она остаётся `running` навсегда: воспроизведено во всех
        18 прогонах матрицы аварий. Само по себе это ничего не ломает
        (`last_successful_run` смотрит только на `ok`/`partial`), но
        кладбище растёт вечно и никем не показывается — то есть авария не
        оставляет следа ровно там, где след и нужен.

        Вызывается под замком прогона (`runlock.single_run`), и это не
        деталь, а условие корректности: пока замок держит один процесс,
        любая строка `running` заведомо принадлежит уже мёртвому.
        `finished_at` не выдумывается — время смерти неизвестно, и
        подставить сюда «сейчас» значило бы соврать о длительности.
        """
        cursor = self._connection.execute(
            "UPDATE run SET status = 'interrupted', "
            "error = COALESCE(error, 'процесс умер, не закрыв строку журнала') "
            "WHERE status = 'running'"
        )
        self._connection.commit()
        abandoned = int(cursor.rowcount)
        if abandoned:
            logger.warning(
                "%d строк журнала остались в статусе running от прошлых процессов "
                "(SIGKILL, OOM-kill или `docker kill` посреди прогона) и помечены "
                "interrupted; успешными они не считались и раньше",
                abandoned,
            )
        return abandoned

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

        Дата из БУДУЩЕГО пропускается по той же логике, что и нечитаемая.
        Часы, ушедшие вперёд (правка времени на хосте, кривой RTC, скачок
        NTP), оставляют в журнале строку с `finished_at` на месяцы вперёд —
        и она делает healthcheck зелёным НАВСЕГДА: он сравнивает
        `last < deadline`, а будущее `last` меньше порога не бывает. Тот же
        след ломает и предохранитель `_too_soon`: возраст прогона
        отрицательный, значит «прошлый был только что» — и прогоны
        пропускаются вечно. Обе беды тихие, поэтому строка не просто
        игнорируется, а объявляется вслух.
        """
        cursor = self._connection.execute(
            "SELECT CAST(finished_at AS BLOB) AS finished_at FROM run "
            "WHERE status IN ('ok', 'partial') AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC"
        )
        horizon = datetime.now(UTC) + CLOCK_SKEW_TOLERANCE
        for row in cursor:
            raw = row["finished_at"]
            try:
                finished = parse_utc(decode_text(raw))
            except ValueError:
                logger.error(
                    "журнал прогонов: finished_at = %r не разбирается как дата, строка пропущена",
                    raw,
                    exc_info=True,
                )
                continue
            if finished > horizon:
                logger.error(
                    "журнал прогонов: прогон завершён %s — это будущее, часы уходили вперёд. "
                    "Строка пропущена: иначе healthcheck считал бы сервис здоровым до самой "
                    "этой даты, а прогоны пропускались бы как «только что сделанные»",
                    finished.isoformat(),
                )
                continue
            return finished
        return None

    def last_run(self) -> "RunSummary | None":
        """Последний прогон ЛЮБОГО исхода — для диагностики, не для вердикта.

        Счётчики прогона таблица `run` копила с самого начала, и до сих пор
        их не читал никто: единственная выборка из `run` брала `finished_at`
        успешных. То есть наблюдаемость была только на запись. Здесь она
        появляется на чтение — ровно в той команде, которую человек
        выполняет, когда индикатор покраснел, и которую дёргает Docker.

        Дата не разбирается вовсе: строка нужна для показа человеку, и
        нечитаемое значение здесь не имеет права ничего уронить.
        """
        row = self._connection.execute(
            "SELECT CAST(status AS BLOB) AS status, CAST(finished_at AS BLOB) AS finished_at, "
            "CAST(error AS BLOB) AS error, CAST(discovered AS BLOB) AS discovered, "
            "CAST(enriched AS BLOB) AS enriched, CAST(reported AS BLOB) AS reported "
            "FROM run ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return RunSummary(
            status=_shown(row["status"]) or "неизвестен",
            finished_at=_shown(row["finished_at"]),
            error=_shown(row["error"]),
            discovered=_shown(row["discovered"]) or "0",
            enriched=_shown(row["enriched"]) or "0",
            reported=_shown(row["reported"]) or "0",
        )

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


def _shown(value: object) -> str | None:
    """Значение из журнала в виде текста; нечитаемое — как есть, в repr.

    Ничего не бросает: показ диагностики не имеет права упасть на порче
    ровно тех данных, ради разбора которых его и открыли.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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
