"""Discovery через курируемый листинг hh.ru — единственный разрешённый путь.

Живой robots.txt hh.ru в группе `User-agent: *` содержит правило
`Disallow: *?*` — запрет ЛЮБОГО URL с query-строкой. Под него целиком
попадает RSS-поиск (`/search/vacancy/rss?text=...`), на котором стоял
шаг discovery до этого раунда. Проверено живой загрузкой 2026-07-28,
фикстура — `tests/fixtures/robots_hh.txt`.

Разрешённых форм ровно две, обе проверены матчером на живых правилах:

    /vacancies/{slug}            — правил не совпало, значит разрешено
    /vacancies/{slug}?page=N     — `Allow: /vacancies/*?page=` (длиннее
                                   запрета `*?*`, поэтому побеждает)

Форма `/vacancies/{slug}?area=66&page=1` формально прошла бы под правило
`Allow: /vacancies/*?*&page=`, но это обход духа запрета, а не его
соблюдение: query-строку hh.ru закрыл сознательно. Такие URL здесь не
строятся, а `QuerySpec.slug` отвергает символы `?`, `&`, `/` и `#` на
старте (см. `config/models.py`).

Ценой переезда стал объём данных: листинг отдаёт только id, url и
заголовок. Компания, регион, зарплата и дата публикации приходят теперь
на шаге обогащения, со страницы вакансии.
"""

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy
from hh_search.errors import FetchFailed
from hh_search.sources.vacancy_page import find_ld_json, vacancy_url

logger = logging.getLogger(__name__)

LISTING_BASE_URL = "https://hh.ru/vacancies"
# Сколько причин пропуска попадает в лог целиком: страница отдаёт 20
# элементов, и однотипных причин там обычно одна-две.
_MAX_LOGGED_REASONS = 5

_ID_RE = re.compile(r"^/vacancy/(\d+)$")
_CANONICAL_RE = re.compile(r"<link[^>]+rel=[\"']canonical[\"'][^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']")


def build_listing_url(query: QuerySpec, page: int = 0) -> str:
    """URL страницы листинга. Нумерация страниц у hh.ru с нуля.

    Первая страница — голый путь без query-строки: `?page=0` попал бы под
    `Disallow: *?*`, не получив защиты от `Allow: /vacancies/*?page=`...
    точнее, получив её, но заплатив за это лишним query-параметром там,
    где он не нужен. Голый путь не совпадает вообще ни с одним правилом.
    """
    if page < 0:
        raise ValueError(f"номер страниц не может быть отрицательным: {page}")
    base = f"{LISTING_BASE_URL}/{query.slug}"
    return base if page == 0 else f"{base}?page={page}"


def _canonical_path(html: str) -> str | None:
    """Путь из `<link rel="canonical">`. У второй страницы там ещё и `?page=2`,
    поэтому сравнивается именно путь, а не URL целиком."""
    tag = _CANONICAL_RE.search(html)
    if tag is None:
        return None
    href = _HREF_RE.search(tag.group(0))
    if href is None:
        return None
    return urlsplit(href.group(1)).path.rstrip("/")


def _check_slug(html: str, slug: str) -> None:
    """Сторож подмены листинга общим индексом.

    Несуществующий slug hh.ru не отдаёт 404: он молча показывает общий
    индекс `/vacancies` со статусом 200 и полным ItemList из двадцати
    посторонних вакансий (проверено на `/vacancies/yocto` — приходят
    кладовщик и сборщик заказов). Без этой проверки опечатка в конфиге
    тихо заливала бы базу мусором, который потом честно обогащается,
    оценивается и попадает в отчёт.
    """
    expected = f"/vacancies/{slug}"
    actual = _canonical_path(html)
    if actual is None:
        raise FetchFailed(
            f"на странице листинга {expected} нет тега <link rel=\"canonical\">: "
            "проверить, что hh.ru отдал запрошенный листинг, а не общий индекс, нечем"
        )
    if actual != expected:
        raise FetchFailed(
            f"запрошен листинг {expected}, а hh.ru отдал {actual!r}. "
            f"Скорее всего, slug {slug!r} не существует — hh.ru не отвечает на такой "
            "404, а молча показывает общий индекс вакансий"
        )


def _item_list(html: str, slug: str) -> list[Any]:
    """Элементы блока JSON-LD с `@type: ItemList`. Дрейф формата — отказ."""
    block = find_ld_json(html, "ItemList")
    if block is None:
        raise FetchFailed(
            f"на странице листинга {slug!r} нет блока JSON-LD с ItemList — "
            "похоже, разметка страницы изменилась"
        )
    items = block.get("itemListElement")
    if not isinstance(items, list):
        raise FetchFailed(
            f"листинг {slug!r}: itemListElement не список, а "
            f"{type(items).__name__} — похоже, разметка страницы изменилась"
        )
    return items


def _parse_item(raw: Any, query_text: str) -> tuple[DiscoveredVacancy | None, str | None]:
    """Один ListItem: либо вакансия, либо причина пропуска."""
    if not isinstance(raw, dict):
        return None, f"элемент списка не объект, а {type(raw).__name__}"
    url = raw.get("url")
    if not isinstance(url, str):
        return None, f"нет строкового поля url: {url!r}"
    match = _ID_RE.match(urlsplit(url).path)
    if match is None:
        return None, f"url не похож на ссылку на вакансию: {url!r}"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        # Заголовок идёт в скоринг с самым большим весом: пустая строка не
        # «вакансия без названия», а разобранный наполовину элемент, и
        # записывать её значит навсегда зафиксировать нулевой вклад title.
        return None, f"нет непустого заголовка у вакансии {match.group(1)}: {name!r}"
    vacancy_id = match.group(1)
    return (
        DiscoveredVacancy(
            id=vacancy_id,
            # URL из ленты не переиспользуется: он может нести utm-хвост, а
            # любая query-строка запрещена robots.txt. Собираем канонический.
            url=vacancy_url(vacancy_id),
            title=name.strip(),
            found_by_query=query_text,
        ),
        None,
    )


def parse_listing(html: str, query_text: str) -> list[DiscoveredVacancy]:
    """Разбирает страницу листинга. `query_text` — запрошенный slug.

    Он же попадает в `found_by_query`: после переезда на листинги запрос
    и есть slug, отдельного текста поиска больше не существует.

    Громкий отказ обязателен в трёх случаях: страница оказалась другим
    листингом (несуществующий slug), блока ItemList нет вовсе, элементы
    есть — но не разобрался ни один. Ноль элементов на честно пустой
    выдаче остаётся законным результатом.
    """
    _check_slug(html, query_text)
    items = _item_list(html, query_text)

    vacancies: list[DiscoveredVacancy] = []
    skipped: list[str] = []
    for raw in items:
        vacancy, reason = _parse_item(raw, query_text)
        if vacancy is None:
            skipped.append(reason or "")
            continue
        vacancies.append(vacancy)

    if skipped:
        logger.warning(
            "листинг %r: пропущено %d из %d элементов; причины: %s",
            query_text,
            len(skipped),
            len(items),
            "; ".join(skipped[:_MAX_LOGGED_REASONS]),
        )
    if items and not vacancies:
        raise FetchFailed(
            f"листинг {query_text!r}: разобрать не удалось ни один из {len(items)} "
            f"элементов — похоже, формат страницы изменился. "
            f"Причины: {'; '.join(skipped[:_MAX_LOGGED_REASONS])}"
        )
    return vacancies
