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

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        """Отдать вакансии приёмнику. Возвращает, сколько ЗАПИСАНО.

        Не «сколько отдано»: оба файловых приёмника дедуплицируют по
        содержимому файла дня, поэтому пачка из 143 вакансий регулярно
        превращается в ноль новых строк. Пока число не возвращалось,
        команда `report` печатала «перегенерировано вакансий: 143», не
        записав ни байта (замер: размеры файлов до и после совпали до
        байта), — сообщение называло не то, что произошло.
        """
        ...
