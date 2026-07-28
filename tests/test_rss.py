import logging
from datetime import datetime
from pathlib import Path

import pytest

from hh_search.errors import FetchFailed
from hh_search.sources.rss import RssQuery, build_rss_url, parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "rss_yocto.xml"


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


# --- Раунд исправлений 3: дрейф формата ленты обязан быть громким ----------


def _feed(items: str) -> str:
    return f'<rss version="2.0"><channel><title>t</title>{items}</channel></rss>'


_ITEM_RFC822 = """
    <item>
        <pubDate>Mon, 27 Jul 2026 14:48:48 +0300</pubDate>
        <title>Инженер</title>
        <link>https://hh.ru/vacancy/1</link>
        <description><![CDATA[<p>Вакансия компании: X</p>]]></description>
    </item>
"""
_ITEM_OK = """
    <item>
        <pubDate>2026-07-27T14:48:48.366+03:00</pubDate>
        <title>OK</title>
        <link>https://hh.ru/vacancy/2</link>
        <description><![CDATA[<p>Вакансия компании: Y</p>]]></description>
    </item>
"""


def test_parse_feed_raises_when_every_item_is_skipped() -> None:
    # RFC 822 — формат pubDate, предписанный RSS 2.0; hh.ru сейчас отдаёт ISO 8601.
    # Дрейф на предписанный формат не имеет права выглядеть как «новых вакансий нет».
    with pytest.raises(FetchFailed, match="ни один"):
        parse_feed(_feed(_ITEM_RFC822 * 3), "Yocto")


def test_parse_feed_raises_when_feed_is_namespaced() -> None:
    xml_text = (
        '<rss version="2.0" xmlns="http://backend.userland.com/rss2">'
        f"<channel>{_ITEM_OK}</channel></rss>"
    )
    with pytest.raises(FetchFailed):
        parse_feed(xml_text, "Yocto")


def test_parse_feed_raises_on_malformed_xml() -> None:
    with pytest.raises(FetchFailed):
        parse_feed("<rss><channel><item></channel></rss>", "Yocto")


def test_parse_feed_logs_reason_for_every_skipped_item(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="hh_search.sources.rss"):
        vacancies = parse_feed(_feed(_ITEM_RFC822 + _ITEM_OK), "Yocto")
    assert [v.id for v in vacancies] == ["2"]
    assert caplog.records, "пропуск элемента обязан оставлять след в логе"
    message = caplog.records[0].getMessage()
    assert "pubDate" in message
    assert "Yocto" in message


def test_parse_feed_accepts_genuinely_empty_feed() -> None:
    # Пустая выдача — законный результат: по узкому запросу может не быть вакансий.
    assert parse_feed(_feed(""), "Yocto") == []


def test_build_rss_url_includes_filters_and_date_ordering() -> None:
    url = build_rss_url(
        RssQuery(
            text="Backend Team Lead",
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
    url = build_rss_url(RssQuery(text="Yocto"))
    assert "area=" not in url
    assert "schedule=" not in url
