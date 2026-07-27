import json
import re
from datetime import datetime
from html import unescape
from typing import Any

from hh_search.domain.models import VacancyDetails
from hh_search.errors import FetchFailed

_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
_BLOCK_END_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def vacancy_url(vacancy_id: str) -> str:
    return f"https://hh.ru/vacancy/{vacancy_id}"


def extract_job_posting(html: str) -> dict[str, Any] | None:
    """Находит блок JSON-LD с типом JobPosting. Битые блоки пропускает."""
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def html_to_text(html: str) -> str:
    text = _BLOCK_END_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _extract_locality(posting: dict[str, Any]) -> str | None:
    location = posting.get("jobLocation")
    if not isinstance(location, dict):
        return None
    address = location.get("address")
    if not isinstance(address, dict):
        return None
    locality = address.get("addressLocality")
    return locality if isinstance(locality, str) else None


def _extract_description(posting: dict[str, Any]) -> str:
    """Описание из JSON-LD. Пустоты не бывает: её нельзя ни записать, ни повторить.

    Обогащение выбирается по `description IS NULL`, поэтому записанная пустая
    строка — необратимый приговор: вакансия навсегда числится обогащённой, с
    баллом 0 и без единой попытки перекачать страницу. Структурная поломка
    JSON-LD накрывает при этом весь бэклог сразу, поэтому любое неожиданное
    значение — отказ, включающий штатный счётчик enrich_attempts.
    """
    if "description" not in posting:
        raise FetchFailed("в JSON-LD JobPosting нет поля description")
    raw_description = posting["description"]
    if not isinstance(raw_description, str):
        raise FetchFailed(
            "поле description в JSON-LD JobPosting не строка, "
            f"а {type(raw_description).__name__}"
        )
    text = html_to_text(raw_description)
    if not text:
        raise FetchFailed("поле description в JSON-LD JobPosting пусто после снятия разметки")
    return text


def parse_vacancy_page(html: str) -> VacancyDetails:
    posting = extract_job_posting(html)
    if posting is None:
        raise FetchFailed("на странице нет блока JSON-LD с JobPosting")
    return VacancyDetails(
        description=_extract_description(posting),
        valid_through=_parse_datetime(posting.get("validThrough")),
        location=_extract_locality(posting),
    )
