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
    assert parse_salary("от\xa0200\xa0000\xa0руб.").amount_from == 200000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("от 250 000 ₽", (250000, None, "₽")),
        ("от 900 000 до 1 300 000 ₸", (900000, 1300000, "₸")),
        ("от\xa0100\xa0000\xa0₽", (100000, None, "₽")),
        ("до 2000 EUR", (None, 2000, "EUR")),
        ("до 2000 USD", (None, 2000, "USD")),
    ],
)
def test_parse_salary_extracts_currency_symbols(
    raw: str, expected: tuple[int | None, int | None, str | None]
) -> None:
    salary = parse_salary(raw)
    assert (salary.amount_from, salary.amount_to, salary.currency) == expected


@pytest.mark.parametrize(
    ("raw", "expected_currency"),
    [
        ("от 200 000 до 300 000 руб. на руки", "руб."),
        ("до 2000 EUR на руки", "EUR"),
    ],
)
def test_parse_salary_ignores_trailing_words_after_currency(
    raw: str, expected_currency: str
) -> None:
    assert parse_salary(raw).currency == expected_currency


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


def test_parse_feed_extracts_salary_from_fixture() -> None:
    vacancies = parse_feed(FIXTURE.read_text(encoding="utf-8"), "Yocto")
    priced = [v for v in vacancies if v.salary.raw and "не указан" not in v.salary.raw]
    assert priced, "fixture must contain at least one vacancy with a stated income"
    for vacancy in priced:
        assert vacancy.salary.currency
        assert vacancy.salary.amount_from is not None or vacancy.salary.amount_to is not None


def test_parse_feed_skips_item_with_invalid_pub_date() -> None:
    xml_text = """<rss version="2.0"><channel>
        <item>
            <pubDate></pubDate>
            <title>Broken</title>
            <link>https://hh.ru/vacancy/1</link>
            <description><![CDATA[<p>Вакансия компании: X</p>]]></description>
        </item>
        <item>
            <pubDate>2026-07-27T14:48:48.366+03:00</pubDate>
            <title>OK</title>
            <link>https://hh.ru/vacancy/2</link>
            <description><![CDATA[<p>Вакансия компании: Y</p>]]></description>
        </item>
    </channel></rss>"""
    vacancies = parse_feed(xml_text, "Yocto")
    assert len(vacancies) == 1
    assert vacancies[0].id == "2"


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
