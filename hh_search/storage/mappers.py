"""Маппинг `sqlite3.Row` в доменные модели.

Вынесено из repository.py: функция используется и `pending_enrichment`,
и `unreported` — раньше жила там же приватным статическим методом.

Текстовые колонки приходят как `bytes` (запрос делает `CAST(... AS
BLOB)`, см. repository.py) — decode здесь, а не в SQLite, чтобы битые
байты не роняли fetch всей строки, а превращались в обычный
`UnicodeDecodeError`, который вызывающий код (repository.py) ловит и
карантинирует по месту.
"""

import sqlite3

from hh_search.domain.models import DiscoveredVacancy, Salary
from hh_search.storage.time_utils import parse_utc


def decode_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def decode_optional_text(value: bytes | str | None) -> str | None:
    return None if value is None else decode_text(value)


def to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=row["id"],
        url=decode_text(row["url"]),
        title=decode_text(row["title"]),
        company=decode_optional_text(row["company"]),
        area=decode_optional_text(row["area"]),
        salary=Salary(
            raw=decode_optional_text(row["salary_raw"]),
            amount_from=row["salary_from"],
            amount_to=row["salary_to"],
            currency=decode_optional_text(row["salary_currency"]),
        ),
        published_at=parse_utc(decode_text(row["published_at"])),
        found_by_query=decode_text(row["primary_query"]),
    )
