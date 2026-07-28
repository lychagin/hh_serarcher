"""RSS-поиск hh.ru. КОНВЕЙЕРОМ НЕ ИСПОЛЬЗУЕТСЯ — запрещён robots.txt.

Почему выключен
---------------
Живой robots.txt hh.ru в группе `User-agent: *` содержит правило
`Disallow: *?*` — запрет любого URL с query-строкой. RSS-поиск без
query-строки не существует (`?text=...&order_by=publication_time`),
поэтому под запрет попадает весь источник целиком, а не отдельные его
режимы. Проверено живой загрузкой 2026-07-28; правила лежат в
`tests/fixtures/robots_hh.txt`, вердикт закреплён тестом
`test_live_robots_forbids_rss_search`.

До коммита `0c5c397` запрет был невидим: `urllib.robotparser`
сравнивала только путь, без query-строки, и `respect_robots: true`
давал ложную уверенность. Свой матчер RFC 9309 запрет видит, то есть
запрос физически не уйдёт в сеть — модуль стал неработоспособен не по
решению автора, а по факту.

Почему не удалён
----------------
robots.txt — это правила источника, а не свойство мира: hh.ru может их
изменить. Если `Disallow: *?*` уйдёт, RSS вернётся как источник
discovery на порядок более богатый, чем листинг (он отдавал компанию,
регион, зарплату и дату публикации сразу, без похода на страницу
вакансии). Восстановление тогда стоит одного вызова, а не переписывания.

Discovery живёт в `listing.py`. Разбор зарплаты переехал в `salary.py`
и продолжает использоваться — страница вакансии отдаёт её в том же
формате; здесь он импортируется, чтобы `parse_feed` остался рабочим.
"""

import logging
import re
from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from hh_search.domain.models import DiscoveredVacancy
from hh_search.errors import FetchFailed
from hh_search.sources.salary import parse_salary

logger = logging.getLogger(__name__)

RSS_BASE_URL = "https://hh.ru/search/vacancy/rss"
# Сколько причин пропуска попадает в лог целиком: лента отдаёт максимум 20
# элементов, и однотипных причин там обычно одна-две.
_MAX_LOGGED_REASONS = 5

_ID_RE = re.compile(r"/vacancy/(\d+)")
_COMPANY_RE = re.compile(r"Вакансия компании:\s*([^<]+)")
_REGION_RE = re.compile(r"Регион:\s*([^<]+)")
_INCOME_RE = re.compile(r"дохода:\s*([^<]+)")

__all__ = ["RSS_BASE_URL", "RssQuery", "build_rss_url", "parse_feed", "parse_salary"]


class RssQuery(BaseModel):
    """Параметры RSS-поиска.

    Раньше это были поля `QuerySpec`, то есть часть пользовательского
    конфига. После переезда discovery на листинг они потеряли смысл:
    листинг не принимает ни текста поиска, ни региона, ни опыта — любой
    такой параметр стал бы query-строкой, а она запрещена. Чтобы конфиг
    не описывал возможностей, которых нет, описание RSS-запроса переехало
    сюда, к самому RSS.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    area: list[int] | None = None
    experience: list[str] | None = None
    employment: str | None = None
    schedule: str | None = None
    period: int | None = Field(default=None, ge=0, le=30)


def build_rss_url(query: RssQuery) -> str:
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


def _first_group(regex: re.Pattern[str], text: str) -> str | None:
    match = regex.search(text)
    return match.group(1).strip() if match else None


def parse_feed(xml_text: str, query_text: str) -> list[DiscoveredVacancy]:
    """Разбирает ленту. Дрейф формата обязан отказывать громко, а не тихо пустеть.

    Пропуск отдельного элемента — законная устойчивость (одна битая запись не
    уносит остальные 19), но он всегда попадает в лог с причиной. Если у
    НЕПУСТОЙ ленты не разобрался НИ ОДИН элемент, это уже не битая запись, а
    смена формата: hh.ru отдаёт pubDate в ISO 8601, но RSS 2.0 предписывает
    RFC 822, и переход на предписанный формат не имеет права выглядеть как
    «новых вакансий нет» — иначе сервис молчит месяцами при зелёном
    healthcheck. Пустая лента (0 элементов) — законный результат узкого запроса.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise FetchFailed(
            f"лента по запросу {query_text!r} не разбирается как XML: {error}"
        ) from error
    if root.tag != "rss":
        # Пространство имён по умолчанию превращает тег в «{uri}rss», и тогда
        # ./channel/item молча не находит ничего.
        raise FetchFailed(
            f"лента по запросу {query_text!r}: корневой элемент <{root.tag}>, ожидался <rss>"
        )
    if root.find("channel") is None:
        raise FetchFailed(f"лента по запросу {query_text!r}: нет элемента <channel>")

    items = list(root.iterfind("./channel/item"))
    vacancies: list[DiscoveredVacancy] = []
    skipped: list[str] = []
    for item in items:
        link = (item.findtext("link") or "").strip()
        id_match = _ID_RE.search(link)
        if not id_match:
            skipped.append(f"в <link> нет id вакансии: {link!r}")
            continue
        raw_published_at = (item.findtext("pubDate") or "").strip()
        try:
            published_at = datetime.fromisoformat(raw_published_at)
        except ValueError:
            skipped.append(f"<pubDate> не разбирается как ISO 8601: {raw_published_at!r}")
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

    if skipped:
        logger.warning(
            "лента по запросу %r: пропущено %d из %d элементов; причины: %s",
            query_text,
            len(skipped),
            len(items),
            "; ".join(skipped[:_MAX_LOGGED_REASONS]),
        )
    if items and not vacancies:
        raise FetchFailed(
            f"лента по запросу {query_text!r}: разобрать не удалось ни один из "
            f"{len(items)} элементов — похоже, формат ленты изменился. "
            f"Причины: {'; '.join(skipped[:_MAX_LOGGED_REASONS])}"
        )
    return vacancies
