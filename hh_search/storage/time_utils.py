"""Единая нормализация дат для слоя хранения.

Всё, что попадает в БД, приводится к aware UTC перед записью и
возвращается как aware UTC при чтении. Это нужно для двух вещей:
лексикографическое сравнение ISO-строк в SQL (`ORDER BY ... DESC`)
совпадает с хронологическим только если все строки в одном смещении,
и потребители (например, планировщик) не должны получать то наивный,
то aware `datetime` из одного и того же метода.
"""

from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def to_utc_iso(moment: datetime) -> str:
    """Наивная дата трактуется как UTC, aware — приводится к UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    else:
        moment = moment.astimezone(UTC)
    return moment.isoformat()


def parse_utc(value: str) -> datetime:
    """Обратная операция к `to_utc_iso`: всегда возвращает aware UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed
