"""Маппинг `sqlite3.Row` в доменные модели — по одному на выборку.

Текстовые колонки приходят как `bytes` (запрос делает `CAST(... AS
BLOB)`, см. repository.py) — decode здесь, а не в SQLite, чтобы битые
байты не роняли fetch всей строки, а превращались в обычный
`UnicodeDecodeError`, который `safe_rows` ловит и разбирает по месту.

Каждая функция здесь — «построитель» для `safe_rows`: она либо строит
модель, либо бросает исключение из `CORRUPTION_EXCEPTIONS`. Порядок
операций в `to_scored` значим: сначала то, что невосстановимо
(discovery-поля, описание), потом оценка — иначе строку с двумя видами
порчи сразу лечили бы локальным пересчётом, который её не спасёт.
"""

import json
import sqlite3

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.storage.quarantine import CORRUPTION_EXCEPTIONS, ScoreUnreadable
from hh_search.storage.time_utils import parse_utc


def decode_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def decode_optional_text(value: bytes | str | None) -> str | None:
    return None if value is None else decode_text(value)


def decode_optional_int(value: bytes | str | None) -> int | None:
    """Числовые колонки тоже приходят как BLOB и тоже бывают испорчены.

    SQLite типизирован динамически: в колонке INTEGER может лежать текст,
    в том числе невалидный UTF-8, — и тогда fetch роняет ВЕСЬ курсор
    ровно так же, как на текстовой колонке. Поэтому CAST(... AS BLOB)
    накрывает и их, а разбор в число происходит здесь, где ValueError
    ловится по одной строке.
    """
    return None if value is None else int(decode_text(value))


def to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=decode_text(row["id"]),
        url=decode_text(row["url"]),
        title=decode_text(row["title"]),
        company=decode_optional_text(row["company"]),
        area=decode_optional_text(row["area"]),
        salary=Salary(
            raw=decode_optional_text(row["salary_raw"]),
            amount_from=decode_optional_int(row["salary_from"]),
            amount_to=decode_optional_int(row["salary_to"]),
            currency=decode_optional_text(row["salary_currency"]),
        ),
        published_at=parse_utc(decode_text(row["published_at"])),
        found_by_query=decode_text(row["primary_query"]),
    )


def to_scoring_task(row: sqlite3.Row) -> tuple[DiscoveredVacancy, VacancyDetails]:
    """Описание уже скачано, оценки нет — всё для локального пересчёта."""
    return to_discovered(row), VacancyDetails(description=decode_text(row["description"]))


def to_scored(row: sqlite3.Row) -> ScoredVacancy:
    discovered = to_discovered(row)
    details = VacancyDetails(description=decode_text(row["description"]))
    cluster = decode_optional_text(row["cluster"]) or ""
    raw_score_detail = row["score_detail"]
    try:
        score = ScoreBreakdown.model_validate(json.loads(decode_text(raw_score_detail)))
    except CORRUPTION_EXCEPTIONS as corruption:
        raise ScoreUnreadable(raw_score_detail) from corruption
    return ScoredVacancy(discovered=discovered, details=details, score=score, cluster=cluster)
