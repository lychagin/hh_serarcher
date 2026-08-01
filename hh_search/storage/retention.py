"""Уборка: единственное место в проекте, где строки исчезают.

Обе таблицы обслуживаются здесь, вопреки разделению «`vacancy` в
`repository.py`, `run` в `run_log.py`». Причина названа в спеке
2026-08-01 §3.4: вопрос «что и когда исчезает» обязан читаться в ОДНОМ
файле. Разложенный по двум, он отвечался бы наполовину, и половину при
правке забывали бы.

Соединением модуль не владеет и не закрывает его — как `RunLog`.
"""

import sqlite3
from datetime import datetime

from hh_search.storage.base import STATUS_REPORTED
from hh_search.storage.time_utils import to_utc_iso


class Retention:
    """Уборка старых данных. Ручная: демон эти методы не зовёт."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def descriptions_before(self, cutoff: datetime) -> tuple[int, int]:
        """Сколько описаний старше границы и сколько в них БАЙТ.

        `LENGTH(CAST(... AS BLOB))`, а не `LENGTH(...)`: у TEXT-значения
        `LENGTH` считает символы, и на кириллическом описании число вышло
        бы вдвое меньше правды. Уезжает оно человеку как «освободится
        N МБ», то есть ошибка вдвое делает его бесполезным.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(CAST(description AS BLOB))), 0) AS size "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        ).fetchone()
        return (int(row["rows"]), int(row["size"])) if row else (0, 0)

    def forget_descriptions(self, cutoff: datetime) -> int:
        """Обнулить описания отправленных вакансий старше границы.

        Три условия, и каждое несёт свой инвариант. `status='reported'` —
        очередь обогащения отбирает `status='new' AND description IS
        NULL`, и обнули мы описание у `new`, вакансия ушла бы в сеть за
        уже скачанной страницей. `description IS NOT NULL` — делает метод
        идемпотентным: повторный вызов вернёт 0, а не число уже пустых
        строк. `reported_at < ?` — строки с пустым `reported_at` не
        попадают под сравнение с NULL и остаются целы, что верно: дату
        отправки ставит `mark_reported`, и её отсутствие означает
        состояние, которого уборка не понимает.

        Строка остаётся на месте. Она и есть дедупликация: удалённая
        вакансия была бы найдена заново, скачана ещё раз и повторно
        отправлена в Telegram — циклически, пока висит объявление.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET description = NULL "
            "WHERE status = ? AND description IS NOT NULL AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        )
        self._connection.commit()
        return cursor.rowcount

    def count_runs_before(self, cutoff: datetime) -> int:
        """Сколько ЗАКРЫТЫХ строк журнала старше границы."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS rows FROM run WHERE finished_at IS NOT NULL AND finished_at < ?",
            (to_utc_iso(cutoff),),
        ).fetchone()
        return int(row["rows"]) if row else 0

    def forget_runs(self, cutoff: datetime) -> int:
        """Удалить закрытые строки журнала старше границы.

        По `finished_at`, а не по `started_at`: незакрытая строка
        (`running`, оставшаяся от убитого процесса) даты завершения не
        имеет, и удалять её по дате старта значило бы стирать улику ровно
        того отказа, ради которого журнал ведётся. Такие строки закрывает
        `close_abandoned_runs()`, и удалит их следующая уборка.
        """
        cursor = self._connection.execute(
            "DELETE FROM run WHERE finished_at IS NOT NULL AND finished_at < ?",
            (to_utc_iso(cutoff),),
        )
        self._connection.commit()
        return cursor.rowcount

    def vacuum(self) -> None:
        """Ужать файл базы. Без него уборка не даёт ни одного байта.

        SQLite при `UPDATE ... = NULL` помечает страницы свободными и
        оставляет их в файле. Владелец, посмотрев на размер `hh.db` после
        уборки и увидев то же число, решил бы, что команда сломана.

        `commit()` перед вызовом обязателен: `VACUUM` не выполняется
        внутри открытой транзакции. Цена названа в выводе команды —
        база переписывается целиком и на время держит на диске две копии.
        """
        self._connection.commit()
        self._connection.execute("VACUUM")
