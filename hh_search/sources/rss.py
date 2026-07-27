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

# Числовой литерал суммы: обязательно хотя бы одна цифра (иначе совпадение-пустышка
# вроде «до » в «до вычета налогов» стало бы якорем для валюты), опциональный знак и
# опциональная дробная часть — они входят в совпадение, но не в захваченную группу,
# чтобы `_to_int` работал с чистыми цифрами. `\s` в str-паттернах Python уже покрывает
# неразрывный пробел U+00A0, которым разделяет разряды hh.ru.
_AMOUNT = r"(-?\d[\d\s]*)(?:[.,]\d+)?"
# `(?<!\w)` не даёт зацепиться за «от»/«до» внутри слова («рабОТа», «ДОход»).
_FROM_RE = re.compile(r"(?<!\w)от\s*" + _AMOUNT)
_TO_RE = re.compile(r"(?<!\w)до\s*" + _AMOUNT)
# Токен валюты не содержит ни цифр («₽5» → «₽»), ни слеша («₽/мес» → «₽»).
_CURRENCY_RE = re.compile(r"[^\d\s/]+")
_CURRENCY_LEADING = "([{"
_CURRENCY_TRAILING = ",;:)]}"
# Точка не обрезается (она часть «руб.»), но токен из одной пунктуации валютой не является.
# Символы валют ($, €, ₽, ₸, ...) в набор сознательно не входят.
_PUNCTUATION = ".,;:!?…()[]{}«»\"'-–—/\\"
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


def _last_amount(regex: re.Pattern[str], text: str) -> re.Match[str] | None:
    """Последнее совпадение суммы, а не первое: валюта стоит за самой правой суммой."""
    matches = list(regex.finditer(text))
    return matches[-1] if matches else None


def _to_int(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    digits = _DIGITS_ONLY.sub("", match.group(1))
    if not digits.isdigit():
        return None
    try:
        return int(digits)
    except ValueError:
        # CPython отказывается конвертировать литералы длиннее 4300 цифр; битая лента
        # не должна ронять разбор всех остальных вакансий.
        return None


def _currency_after_amount(
    text: str, from_match: re.Match[str] | None, to_match: re.Match[str] | None
) -> str | None:
    """Валюта — первый бесцифровой токен после конца самого правого числового литерала.

    Якорь существует только там, где регулярка суммы реально распознала число
    (в `_AMOUNT` цифра обязательна), поэтому ни служебные слова диапазона, ни хвостовой
    текст («до вычета налогов», «на руки»), ни посторонние цифры правее валюты
    («обсуждается ... 2») сдвинуть точку отсчёта не могут.
    """
    ends = [m.end() for m in (from_match, to_match) if m is not None]
    if not ends:
        return None
    token_match = _CURRENCY_RE.match(text[max(ends) :].lstrip())
    if token_match is None:
        return None
    token = token_match.group().lstrip(_CURRENCY_LEADING).rstrip(_CURRENCY_TRAILING)
    return token if token.strip(_PUNCTUATION) else None


def parse_salary(raw: str) -> Salary:
    text = (raw or "").strip()
    if not text or "не указан" in text:
        return Salary(raw=text or None)

    from_match = _last_amount(_FROM_RE, text)
    to_match = _last_amount(_TO_RE, text)
    return Salary(
        raw=text,
        amount_from=_to_int(from_match),
        amount_to=_to_int(to_match),
        currency=_currency_after_amount(text, from_match, to_match),
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
        try:
            published_at = datetime.fromisoformat((item.findtext("pubDate") or "").strip())
        except ValueError:
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
                published_at=published_at,
                found_by_query=query_text,
            )
        )
    return vacancies
