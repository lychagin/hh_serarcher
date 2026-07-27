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
# вроде «до » в «до вычета налогов» заняло бы слот суммы), опциональный знак и
# опциональная дробная часть — они входят в совпадение, но не в захваченную группу,
# чтобы `_to_int` работал с чистыми цифрами. `\s` в str-паттернах Python уже покрывает
# неразрывный пробел U+00A0, которым hh.ru разделяет разряды.
_AMOUNT = r"(-?\d[\d\s]*)(?:[.,]\d+)?"
# Токен валюты не содержит ни цифр («₽5» → «₽»), ни слеша («₽/мес» → «₽»).
_CURRENCY = r"([^\d\s/]+)?"
# Перед выражением допускается только пробел и открывающая пунктуация — «(от 100 000 ₽)»
# разбирается, а проза перед суммой («з/п от 100 000 ₽») намеренно нет: пропуск прозы
# вернул бы поиск «первого похожего места», из-за которого «опыт от 3 лет, зарплата
# от 100 000 ₽» разобралось бы как 3 «лет».
_LEAD = r"^[\s(\[{«\"']*"
# Поле дохода разбирается ОДНИМ якорным совпадением от начала строки: нижняя граница,
# верхняя граница, валюта. Всё, что идёт после валюты, в совпадение не входит и на
# результат повлиять не может — хвостовая проза («опыт от 3 лет», «до 31 декабря»,
# «на руки») перестаёт быть входными данными по построению, а не по счастливому
# совпадению формы шаблона hh.ru.
_SALARY_RE = re.compile(
    _LEAD + r"(?:от\s*" + _AMOUNT + r")?\s*(?:до\s*" + _AMOUNT + r")?\s*" + _CURRENCY
)
_CURRENCY_LEADING = "([{"
_CURRENCY_TRAILING = ",;:)]}"
# Точка не обрезается (она часть «руб.»), но токен из одной пунктуации валютой не является.
# Символы валют ($, €, ₽, ₸, ...) в набор сознательно не входят.
_PUNCTUATION = ".,;:!?…()[]{}«»\"'-–—/\\"
# Ключевые слова самой грамматики зарезервированы: «от 100 000 до вычета налогов» не
# должно давать currency='до'.
_RANGE_KEYWORDS = frozenset({"от", "до"})
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


def _to_int(digits: str | None) -> int | None:
    """Значение суммы из захваченной группы; невалидное значение — не значение."""
    if digits is None:
        return None
    cleaned = _DIGITS_ONLY.sub("", digits)
    if not cleaned.isdigit():
        # Отрицательная сумма зарплатой не является: литерал распознан (и слот занят),
        # но значения из него не будет.
        return None
    try:
        return int(cleaned)
    except ValueError:
        # CPython отказывается конвертировать литералы длиннее 4300 цифр; битая лента
        # не должна ронять разбор всех остальных вакансий.
        return None


def _currency(token: str | None, *, has_amount: bool) -> str | None:
    """Валюта из слота грамматики — только если в строке распознана хотя бы одна сумма."""
    if token is None or not has_amount:
        return None
    cleaned = token.lstrip(_CURRENCY_LEADING).rstrip(_CURRENCY_TRAILING)
    if not cleaned.strip(_PUNCTUATION) or cleaned.lower() in _RANGE_KEYWORDS:
        return None
    return cleaned


def parse_salary(raw: str) -> Salary:
    text = (raw or "").strip()
    # Проверка тоже якорная: «не указан» ГДЕ-ТО в строке (например в хвосте
    # «... (оклад не указан явно)») не имеет права обнулять распознанную сумму.
    if not text or text.startswith("не указан"):
        return Salary(raw=text or None)

    match = _SALARY_RE.match(text)
    if match is None:  # pragma: no cover - все части грамматики опциональны
        return Salary(raw=text)
    amount_from, amount_to, currency = match.groups()
    return Salary(
        raw=text,
        amount_from=_to_int(amount_from),
        amount_to=_to_int(amount_to),
        # Слот суммы занят литералом (`is not None`), даже если значение из него
        # получить нельзя — иначе «от -100 000 ₽» потеряло бы и валюту.
        currency=_currency(currency, has_amount=amount_from is not None or amount_to is not None),
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
