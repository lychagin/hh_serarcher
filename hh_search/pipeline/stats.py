"""Счётчики прогона и его статус.

Статус умеет только УХУДШАТЬСЯ. Причина практическая: шагов, способных
частично отказать, четыре, и каждый писал бы своё значение — последний
затирал бы предыдущие, а `ok` после `partial` означал бы прогон, который
потерял работу и об этом не сказал. Отсюда `degrade()` вместо присваивания
и порядок `ok < partial < failed`.

`partial` считается успехом для `last_successful_run()` (значит и для
healthcheck), поэтому им обозначается только частичная потеря работы. Всё,
что означает «прогон не состоялся» или «работа не делается вовсе», обязано
быть `failed` — иначе получается тот самый зелёный healthcheck при
месяцах молчания.
"""

from typing import TypedDict

from pydantic import BaseModel

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"

_RANK = {OK: 0, PARTIAL: 1, FAILED: 2}

# Коды возврата CLI. `partial` отличается от `failed` не строгостью, а
# содержанием: прогон состоялся, но часть работы потеряна. Cron и
# `docker run` видят ненулевой код в обоих случаях, а человек по коду
# различает два разных разбора. 2 не занят намеренно: его отдаёт click на
# ошибку в аргументах, и там же CLI отдаёт его на ошибку конфига.
EXIT_CODES = {OK: 0, FAILED: 1, PARTIAL: 3}


class RunCounters(TypedDict):
    """Поля таблицы `run`, которые заполняет конвейер.

    Именно TypedDict, а не `dict[str, int | str | None]`: `finish_run`
    принимает счётчики через `**counters`, и словарь с размытым типом
    значений mypy обязан сверять с КАЖДЫМ именованным параметром, включая
    `finished_at: datetime | None`, — три ошибки типа на пустом месте. У
    TypedDict набор ключей известен, поэтому проверка идёт по именам, а
    имя счётчика перестаёт быть строкой, которую можно опечатать
    (`ALLOWED_RUN_COUNTERS` неизвестные имена отбрасывает молча — тест
    сверяет один список с другим).
    """

    discovered: int
    new_count: int
    rejected: int
    enriched: int
    rescored: int
    stuck: int
    requeued: int
    stalled: int
    corrupted: int
    reported: int
    error: str | None


class RunStats(BaseModel):
    """То, что уезжает в таблицу `run` и в код возврата CLI."""

    discovered: int = 0
    new_count: int = 0
    rejected: int = 0
    enriched: int = 0
    rescored: int = 0
    stuck: int = 0
    # Сколько вакансий вернулось из отказа префильтра: правка списка
    # стоп-слов достаёт накопленный бэклог, и это работа прогона, а не
    # тихое событие.
    requeued: int = 0
    # Сколько вакансий выведено из очереди обогащения снижением
    # `enrich.max_attempts`: строка остаётся `new` с пустым описанием и
    # невидима всем трём выборкам. `stuck` её не считает — там
    # `description IS NOT NULL`.
    stalled: int = 0
    # Сколько вакансий ушло в карантин терминально за прогон. Потеря
    # навсегда, и без счётчика она не видна ни в статусе, ни в причине.
    corrupted: int = 0
    reported: int = 0
    status: str = OK
    error: str | None = None

    def degrade(self, status: str, reason: str) -> None:
        """Ухудшить статус прогона и запомнить причину.

        Улучшить статус этим методом нельзя: `ok` после `partial` — это
        потеря, о которой прогон промолчал. Причина сохраняется от самого
        плохого статуса; при равном статусе побеждает первая, потому что
        она обычно и есть корень, а последующие — следствия.
        """
        if _RANK[status] > _RANK[self.status]:
            self.status = status
            self.error = reason
        elif self.error is None:
            self.error = reason

    def counters(self) -> RunCounters:
        """Счётчики для `finish_run`. `status` и `finished_at` — не здесь.

        `status` уезжает отдельным параметром, а `finished_at` конвейер не
        передаёт вовсе: время закрытия ставит хранилище.
        """
        return {
            "discovered": self.discovered,
            "new_count": self.new_count,
            "rejected": self.rejected,
            "enriched": self.enriched,
            "rescored": self.rescored,
            "stuck": self.stuck,
            "requeued": self.requeued,
            "stalled": self.stalled,
            "corrupted": self.corrupted,
            "reported": self.reported,
            "error": self.error,
        }

    def exit_code(self) -> int:
        return EXIT_CODES[self.status]
