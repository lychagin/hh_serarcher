from datetime import datetime
from pathlib import Path

import pytest

from hh_search.config.models import QuerySpec
from hh_search.sources.rss import build_rss_url, parse_feed, parse_salary

FIXTURE = Path(__file__).parent / "fixtures" / "rss_yocto.xml"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("не указан", (None, None, None)),
        ("от 200 000 руб.", (200000, None, "руб.")),
        ("от 100 000 до 150 000 руб.", (100000, 150000, "руб.")),
        ("до 300 000 руб.", (None, 300000, "руб.")),
    ],
)
def test_parse_salary(raw: str, expected: tuple[int | None, int | None, str | None]) -> None:
    salary = parse_salary(raw)
    assert (salary.amount_from, salary.amount_to, salary.currency) == expected


def test_parse_salary_handles_non_breaking_spaces() -> None:
    assert parse_salary("от 200 000 руб.").amount_from == 200000


def test_parse_feed_extracts_every_item() -> None:
    vacancies = parse_feed(FIXTURE.read_text(encoding="utf-8"), "Yocto")
    assert len(vacancies) == 20
    assert all(vacancy.id.isdigit() for vacancy in vacancies)
    assert all(vacancy.found_by_query == "Yocto" for vacancy in vacancies)


def test_parse_feed_extracts_fields_of_first_item() -> None:
    first = parse_feed(FIXTURE.read_text(encoding="utf-8"), "Yocto")[0]
    assert first.url == f"https://hh.ru/vacancy/{first.id}"
    assert first.title
    assert first.company
    assert first.area
    assert isinstance(first.published_at, datetime)


def test_build_rss_url_includes_filters_and_date_ordering() -> None:
    url = build_rss_url(
        QuerySpec(
            text="Backend Team Lead",
            cluster="backend",
            area=[66],
            experience=["between3And6", "moreThan6"],
            employment="full",
        )
    )
    assert url.startswith("https://hh.ru/search/vacancy/rss?")
    assert "text=Backend+Team+Lead" in url
    assert "area=66" in url
    assert "experience=between3And6" in url
    assert "experience=moreThan6" in url
    assert "employment=full" in url
    assert "order_by=publication_time" in url


def test_build_rss_url_omits_unset_filters() -> None:
    url = build_rss_url(QuerySpec(text="Yocto", cluster="embedded"))
    assert "area=" not in url
    assert "schedule=" not in url
