"""Маппинг `sqlite3.Row` в доменные модели.

Вынесено из repository.py: функция используется и `pending_enrichment`,
и `unreported` — раньше жила там же приватным статическим методом.
"""

import sqlite3

from hh_search.domain.models import DiscoveredVacancy, Salary
from hh_search.storage.time_utils import parse_utc


def to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        area=row["area"],
        salary=Salary(
            raw=row["salary_raw"],
            amount_from=row["salary_from"],
            amount_to=row["salary_to"],
            currency=row["salary_currency"],
        ),
        published_at=parse_utc(row["published_at"]),
        found_by_query=row["primary_query"] or "",
    )
