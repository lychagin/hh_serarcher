import gzip
from pathlib import Path

import pytest

from hh_search.errors import FetchFailed
from hh_search.sources.vacancy_page import (
    extract_job_posting,
    html_to_text,
    parse_vacancy_page,
    vacancy_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vacancy.html.gz"


def load_fixture() -> str:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return handle.read()


def test_extracts_job_posting_from_real_page() -> None:
    posting = extract_job_posting(load_fixture())
    assert posting is not None
    assert posting["@type"] == "JobPosting"
    assert posting["title"]
    assert posting["description"]


def test_parse_vacancy_page_returns_plain_text_description() -> None:
    details = parse_vacancy_page(load_fixture())
    assert details.description
    assert "<p>" not in details.description
    assert "&nbsp;" not in details.description


def test_parse_vacancy_page_raises_when_json_ld_is_missing() -> None:
    with pytest.raises(FetchFailed):
        parse_vacancy_page("<html><body>ничего полезного</body></html>")


def test_extract_job_posting_skips_other_ld_json_blocks() -> None:
    html = (
        '<script type="application/ld+json">{"@type": "Organization"}</script>'
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Инженер", "description": "<p>текст</p>"}</script>'
    )
    posting = extract_job_posting(html)
    assert posting is not None
    assert posting["title"] == "Инженер"


def test_extract_job_posting_tolerates_malformed_block() -> None:
    html = (
        '<script type="application/ld+json">{ битый json </script>'
        '<script type="application/ld+json">{"@type": "JobPosting", "description": "ок"}</script>'
    )
    posting = extract_job_posting(html)
    assert posting is not None
    assert posting["description"] == "ок"


def test_html_to_text_unescapes_and_keeps_line_breaks() -> None:
    text = html_to_text("<p>Задачи:</p><ul><li>C++&nbsp;&amp; Linux</li><li>Yocto</li></ul>")
    assert "C++ & Linux" in text
    assert "Yocto" in text
    assert "<" not in text


def test_vacancy_url_is_built_from_id() -> None:
    assert vacancy_url("135586311") == "https://hh.ru/vacancy/135586311"


# Пустое описание — не «вакансия без текста», а сломанный разбор. Записать его
# нельзя: pending_enrichment отбирает по `description IS NULL`, поэтому пустая
# строка навсегда фиксирует вакансию как обогащённую с баллом 0. Отказ обязан
# быть громким, чтобы включился штатный enrich_attempts.
@pytest.mark.parametrize(
    ("raw", "case"),
    [
        ("null", "null"),
        ('{"@value": "текст"}', "словарь"),
        ('["текст"]', "список"),
        ("123", "число"),
        ('""', "пустая строка"),
        ('"<p> </p>"', "разметка без текста"),
    ],
)
def test_parse_vacancy_page_raises_on_unusable_description(raw: str, case: str) -> None:
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        f'"description": {raw}}}</script>'
    )
    with pytest.raises(FetchFailed, match="description"):
        parse_vacancy_page(html)


def test_parse_vacancy_page_raises_when_description_key_is_absent() -> None:
    html = '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    with pytest.raises(FetchFailed, match="description"):
        parse_vacancy_page(html)
