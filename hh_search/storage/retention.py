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

# Форма, в которой `to_utc_iso`/`now_iso` всегда пишут дату: ISO-8601 с
# обязательными секундами и разделителем `T` (Python `isoformat()` на aware
# datetime). Не эвристика, а точное описание того, что кладёт сюда сам
# проект — GLOB (в отличие от LIKE) регистрозависим и не путает `_`/`%` с
# обычными символами, поэтому шаблон надёжно ловит нужную длину и разряды.
_ISO_SHAPE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*"


def _is_valid_iso(column_sql: str) -> str:
    """SQL-условие: значение похоже на ISO-дату И SQLite умеет её разобрать.

    Без этой защиты `column < ?` в SQLite ломается на порядке типов: типы
    сортируются `NULL < INTEGER/REAL < TEXT < BLOB`, поэтому любое ЧИСЛО в
    текстовой колонке меньше любой ISO-строки независимо от значения — тот
    же класс дыры, что чинит `CAST(... AS INTEGER)` у `enrich_attempts` в
    `repository.py`. Для дат `CAST` не спасает (SQLite не умеет привести
    произвольный текст к дате), поэтому форма проверяется `GLOB`'ом — он же
    отсекает пустые и укороченные строки («0», «2026», «» — все
    лексикографически МЕНЬШЕ полной даты и без защиты считались бы
    «старыми»), — а диапазоны (месяц 1–12, час 0–23 и т. д.) проверяет
    встроенный `datetime()`, возвращающий NULL на некорректном значении.
    """
    return f"{column_sql} GLOB '{_ISO_SHAPE_GLOB}' AND datetime({column_sql}) IS NOT NULL"


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
            "FROM vacancy WHERE status = ? AND description IS NOT NULL "
            f"AND {_is_valid_iso('reported_at')} AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        ).fetchone()
        return (int(row["rows"]), int(row["size"])) if row else (0, 0)

    def forget_descriptions(self, cutoff: datetime) -> int:
        """Обнулить описания отправленных вакансий старше границы.

        Условия, и каждое несёт свой инвариант. `status='reported'` —
        очередь обогащения отбирает `status='new' AND description IS
        NULL`, и обнули мы описание у `new`, вакансия ушла бы в сеть за
        уже скачанной страницей. `description IS NOT NULL` — делает метод
        идемпотентным: повторный вызов вернёт 0, а не число уже пустых
        строк. `_is_valid_iso(...)` — испорченный `reported_at` (число,
        пустая строка, обрывок даты — см. докстринг функции) не считается
        «старым»: строка остаётся уликой, а не тихой потерей. `reported_at
        < ?` — строки с пустым `reported_at` не попадают под сравнение с
        NULL и остаются целы, что верно: дату отправки ставит
        `mark_reported`, и её отсутствие означает состояние, которого
        уборка не понимает.

        Строка остаётся на месте. Она и есть дедупликация: удалённая
        вакансия была бы найдена заново, скачана ещё раз и повторно
        отправлена в Telegram — циклически, пока висит объявление.

        Вектор, факты и имена давших их моделей обнуляются ТЕМ ЖЕ
        UPDATE. Всё это — производные описания, а пережившая исходник
        производная суть данные, которые нечем перепроверить: ни
        пересчитать, ни объяснить, откуда взялись. Заодно снимается и
        вес: 4 КБ вектора на вакансию, которые уборка иначе оставляла бы
        на диске навсегда, чистя ради них же описание.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET description = NULL, embedding = NULL, embedding_model = NULL, "
            "llm_facts = NULL, llm_facts_model = NULL "
            "WHERE status = ? AND description IS NOT NULL "
            f"AND {_is_valid_iso('reported_at')} AND reported_at < ?",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        )
        self._connection.commit()
        return cursor.rowcount

    # Граница журнала — `COALESCE(finished_at, started_at)`, а не голый
    # `finished_at`. `close_abandoned_runs()` переводит зависшую строку в
    # `interrupted`, но сознательно НЕ проставляет `finished_at` — время
    # смерти неизвестно, и выдумывать его значило бы соврать (см. докстринг
    # самого `close_abandoned_runs`). С голым `finished_at` это делало
    # обещание спеки «365 дней» ложным: строка `interrupted` НИКОГДА не
    # получала `finished_at`, то есть никогда не попадала под `< cutoff`, и
    # кладбище росло вечно. `started_at` у такой строки есть всегда — он и
    # служит запасной датой для уже закрытых строк.
    #
    # `status != 'running'` — отдельная защита ЖИВОЙ строки, а не даты.
    # Голого `started_at` недостаточно: прогон, идущий часами (или просто
    # начатый до того, как истёк срок хранения журнала), обязан остаться
    # нетронутым, даже если граница по дате старта у него «старая». Уборка
    # берёт тот же замок `single_run`, что и прогон (§3.2 спеки
    # 2026-08-01), поэтому строка `running`, увиденная здесь, — это либо
    # текущий прогон, либо ещё не закрытый `close_abandoned_runs()`; в
    # обоих случаях удалять её раньше — тот же Critical, ради
    # недостижимости которого написана эта защита.
    _RUN_BOUNDARY_SQL = "COALESCE(finished_at, started_at)"

    def count_runs_before(self, cutoff: datetime) -> int:
        """Сколько строк журнала, кроме идущего прогона, старше границы."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS rows FROM run WHERE status != 'running' "
            f"AND {_is_valid_iso(self._RUN_BOUNDARY_SQL)} AND {self._RUN_BOUNDARY_SQL} < ?",
            (to_utc_iso(cutoff),),
        ).fetchone()
        return int(row["rows"]) if row else 0

    def forget_runs(self, cutoff: datetime) -> int:
        """Удалить строки журнала, кроме идущего прогона, старше границы.

        Приём и обоснование границы — в комментарии над
        `_RUN_BOUNDARY_SQL`; `_is_valid_iso(...)` защищает от той же
        порчи типов и укороченных дат, что и `forget_descriptions`, — для
        `run` цена промаха выше: строка не обнуляется, а удаляется, и
        испорченный `finished_at` сам по себе улика.
        """
        cursor = self._connection.execute(
            "DELETE FROM run WHERE status != 'running' "
            f"AND {_is_valid_iso(self._RUN_BOUNDARY_SQL)} AND {self._RUN_BOUNDARY_SQL} < ?",
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
