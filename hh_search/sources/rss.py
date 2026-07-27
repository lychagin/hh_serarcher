import re
from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy, Salary

RSS_BASE_URL = "https://hh.ru/search/vacancy/rss"

_ID_RE = re.compile(r"/vacancy/(\d+)")
_COMPANY_RE = re.compile(r"Вакансия компании:\s*([^<]+)")
_REGION_RE = re.compile(r"Регион:\s*([^<]+)")
_INCOME_RE = re.compile(r"дохода:\s*([^<]+)")
_FROM_RE = re.compile(r"от\s*([\d\s ]+)")
_TO_RE = re.compile(r"до\s*([\d\s ]+)")
_CURRENCY_RE = re.compile(r"([A-Za-zА-Яа-я.]+)\s*$")
_DIGITS_ONLY = re.compile(r"[\s ]")


def build_rss_url(query: QuerySpec) -> str:
    params: list[tuple[str, str]] = [("text", query.text), ("order_by", "publication_time")]
    for area in query.area or []:
        params.append(("area", str(area)))
    for experience in query.experience or []:
        params.append(("experience", experience))
    if query.employment:
        params.append(("employment", query.employment))
    if query.schedule:
        params.append(("schedule", query.schedule))
    if query.period is not None:
        params.append(("period", str(query.period)))
    return f"{RSS_BASE_URL}?{urlencode(params)}"


def parse_salary(raw: str) -> Salary:
    text = (raw or "").strip()
    if not text or "не указан" in text:
        return Salary(raw=text or None)

    def to_int(match: re.Match[str] | None) -> int | None:
        if match is None:
            return None
        digits = _DIGITS_ONLY.sub("", match.group(1))
        return int(digits) if digits.isdigit() else None

    currency_match = _CURRENCY_RE.search(text)
    return Salary(
        raw=text,
        amount_from=to_int(_FROM_RE.search(text)),
        amount_to=to_int(_TO_RE.search(text)),
        currency=currency_match.group(1) if currency_match else None,
    )


def _first_group(regex: re.Pattern[str], text: str) -> str | None:
    match = regex.search(text)
    return match.group(1).strip() if match else None


def parse_feed(xml_text: str, query_text: str) -> list[DiscoveredVacancy]:
    root = ElementTree.fromstring(xml_text)
    vacancies: list[DiscoveredVacancy] = []
    for item in root.iterfind("./channel/item"):
        link = (item.findtext("link") or "").strip()
        id_match = _ID_RE.search(link)
        if not id_match:
            continue
        description = item.findtext("description") or ""
        income = _first_group(_INCOME_RE, description) or ""
        vacancies.append(
            DiscoveredVacancy(
                id=id_match.group(1),
                url=f"https://hh.ru/vacancy/{id_match.group(1)}",
                title=(item.findtext("title") or "").strip(),
                company=_first_group(_COMPANY_RE, description),
                area=_first_group(_REGION_RE, description),
                salary=parse_salary(income),
                published_at=datetime.fromisoformat((item.findtext("pubDate") or "").strip()),
                found_by_query=query_text,
            )
        )
    return vacancies
