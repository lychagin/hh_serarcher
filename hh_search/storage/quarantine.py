"""Обработка порчи данных в таблице `vacancy`.

Порчи бывает ровно два вида, и они лечатся по-разному — вся эта разница и
есть содержание модуля.

1. Не читается ОЦЕНКА (`score`/`score_detail`). Оценка вычисляется
   локально из описания, и описание в этот момент цело. Лечение —
   обнулить только оценку: вакансия попадает в `pending_scoring` и
   пересчитывается без единого обращения к hh.ru. `description` не
   трогаем никогда.
2. Не читаются данные, пришедшие из RSS (`id`, `title`, `published_at`,
   `salary_*`, `area`, `company`, `cluster`) или само описание. Страница
   вакансии этих полей не содержит либо уже была скачана — перекачка их
   не восстановит в принципе, поэтому самовосстановление здесь
   бессмысленно. Лечение — сразу терминальный статус, одна запись в лог,
   ни одна колонка не обнуляется (испорченные значения и есть улики).

Предохранителя-счётчика здесь нет сознательно: он обслуживал сетевой
цикл переобогащения, которого больше не существует. Цикл (2) невозможен —
терминальный статус выводит строку из всех выборок с первого раза.

Цикл (1) целиком локальный (в сеть не ходит ни при какой длине), но
ограничен он НЕ этим модулем: сам по себе он способен повторяться прогон
за прогоном, если запись оценки систематически даёт то, что не читается
обратно. Единственный известный способ такое записать — оценка с
`inf`/`nan`: json пишет их как `null`, а `null` не проходит обратную
валидацию. Поэтому предохранитель поставлен в корне, а не здесь:
`ScoreBreakdown` запрещает `inf`/`nan` на входе
(`allow_inf_nan=False`), то есть нечитаемая оценка не может быть
записана вовсе. Наблюдаемость на случай неизвестного источника такой
записи — счётчики прогона `rescored`/`stuck` в таблице `run`.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterable

STATUS_CORRUPT = "corrupt"

# Все ожидаемые формы порчи — подклассы ValueError: json.JSONDecodeError,
# UnicodeDecodeError, pydantic.ValidationError и ValueError из
# datetime.fromisoformat. TypeError убран из перечня: его единственным
# источником был наш собственный raise при `score_detail IS NULL`, а
# такие строки в `unreported()` больше не попадают вовсе — их забирает
# `pending_scoring()`. Всё, что не ValueError (AttributeError, TypeError
# из сломанного валидатора), — баг в коде, а не порча данных, и обязано
# падать громко, а не тихо стирать оценку здоровой вакансии.
CORRUPTION_EXCEPTIONS = (ValueError,)

logger = logging.getLogger(__name__)


class ScoreUnreadable(ValueError):
    """Не читается только оценка; описание цело и перекачка не нужна.

    Отдельный тип нужен, чтобы `safe_rows` отличил локально излечимую
    порчу от той, для которой нет никакого источника восстановления.
    """

    def __init__(self, payload: bytes | None) -> None:
        super().__init__("score_detail не читается")
        self.payload = payload


class Quarantine:
    """Две операции над испорченной строкой — по одной на вид порчи."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def drop_score(self, key: bytes, payload: bytes | None) -> None:
        """Обнулить ТОЛЬКО оценку: строка уходит в `pending_scoring`.

        `corrupt_payload` пишется через COALESCE — первые сохранённые
        улики не затираются повторным карантином.
        """
        logger.error(
            "вакансия %r: score_detail не читается, оценка обнулена и будет "
            "пересчитана локально; описание сохранено, в сеть не идём",
            key,
            exc_info=True,
        )
        self._connection.execute(
            "UPDATE vacancy SET score = NULL, score_detail = NULL, "
            "corrupt_payload = COALESCE(corrupt_payload, ?) WHERE CAST(id AS BLOB) = ?",
            (payload, key),
        )
        self._connection.commit()

    def terminate(self, key: bytes) -> None:
        """Терминальный статус без обнуления чего бы то ни было."""
        logger.error(
            "вакансия %r: повреждены данные, которых нет на странице вакансии — "
            "перекачка их не восстановит; статус %s, значения оставлены как улики",
            key,
            STATUS_CORRUPT,
            exc_info=True,
        )
        self._connection.execute(
            "UPDATE vacancy SET status = ? WHERE CAST(id AS BLOB) = ?",
            (STATUS_CORRUPT, key),
        )
        self._connection.commit()


def safe_rows[T](
    rows: Iterable[sqlite3.Row],
    build: Callable[[sqlite3.Row], T],
    quarantine: Quarantine,
) -> list[T]:
    """Собрать модели из строк, изолируя порчу одной строки от остальных.

    `id` во всех выборках выбирается как `CAST(id AS BLOB)`, поэтому
    ключ для UPDATE читается даже у строки с испорченным первичным
    ключом, и `WHERE CAST(id AS BLOB) = ?` по нему отрабатывает.
    """
    result: list[T] = []
    for row in rows:
        key: bytes = row["id"]
        try:
            result.append(build(row))
        except ScoreUnreadable as unreadable:
            quarantine.drop_score(key, unreadable.payload)
        except CORRUPTION_EXCEPTIONS:
            quarantine.terminate(key)
    return result
