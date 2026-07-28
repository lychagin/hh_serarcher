from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from hh_search.domain.models import ScoredVacancy

# Формат даты в отчётах: без микросекунд и без смещения. Из базы даты
# приходят как aware UTC (`storage/time_utils.py`), то есть isoformat() дал
# бы «2026-07-27T11:48:48.366000+00:00» — Excel такую строку числом не
# считает, а человеку она нечитаема. Время в отчёте — UTC, как в базе.
REPORT_DATE_FORMAT = "%Y-%m-%d %H:%M"


class Sink(Protocol):
    """Точка расширения: сюда позже встанет TelegramSink."""

    name: str

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None: ...
