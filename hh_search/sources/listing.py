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
строятся, а `QuerySpec.slug` сужен регуляркой `^[a-z0-9][a-z0-9-]*$` и
отвергает всё остальное на старте, до первого запроса (см.
`config/models.py`). Второй половиной той же гарантии служит
`http.normalize_url()`: robots проверяется по НОРМАЛИЗОВАННОМУ URL, поэтому
построить здесь строку, которая проверится как одна, а уйдёт в сеть как
другая, нельзя даже случайно.

Ценой переезда стал объём данных: листинг отдаёт только id, url и
заголовок. Компания, регион, зарплата и дата публикации приходят теперь
на шаге обогащения, со страницы вакансии.
"""

import logging
import re
from typing import Any
from urllib.parse import SplitResult, urlsplit

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy
from hh_search.errors import FetchFailed
from hh_search.sources.vacancy_page import find_ld_json, vacancy_id_from_path, vacancy_url

logger = logging.getLogger(__name__)

LISTING_BASE_URL = "https://hh.ru/vacancies"
# Свой хост — этот и его поддомены (см. `_is_own_host`): и у canonical
# страницы, и у ссылок в ленте. Берётся из LISTING_BASE_URL, чтобы не
# разойтись с ним.
LISTING_HOST = urlsplit(LISTING_BASE_URL).netloc
# Сколько причин пропуска попадает в лог целиком: страница отдаёт 20
# элементов, и однотипных причин там обычно одна-две.
_MAX_LOGGED_REASONS = 5

_CANONICAL_RE = re.compile(r"<link[^>]+rel=[\"']canonical[\"'][^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']")
_HEAD_RE = re.compile(r"<head\b[^>]*>(.*?)</head>", re.DOTALL | re.IGNORECASE)


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


def _is_own_host(url_parts: SplitResult) -> bool:
    """Свой ли хост у разобранного URL. Поддомены hh.ru — свои.

    hh.ru отвечает на `/vacancies/{slug}` редиректом 302 на региональный
    поддомен по геолокации IP: с нижегородского адреса — на `nn.hh.ru`.
    На отданной странице и `<link rel="canonical">`, и ВСЕ двадцать ссылок
    элементов ведут уже на поддомен, поэтому сравнение с одним `hh.ru`
    отбраковывало сначала canonical (шаг падал как «slug не существует»),
    а затем каждый элемент ленты — то есть discovery не работал вовсе ни
    на одном не-московском выходе. Легальность от расширения не страдает:
    robots.txt клиент кэширует по origin и проверяет на каждом хопе
    редиректа, так что для поддомена берутся его собственные правила.

    Расширение обязано остаться сужением, поэтому:

    * сравнивается `hostname`, а не `netloc` — он уже приведён к нижнему
      регистру и очищен от порта и userinfo, так что `https://nn.HH.ru`
      и `https://hh.ru@evil.com` не расходятся с записанным правилом;
    * суффикс проверяется вместе с точкой (`.hh.ru`), иначе `evil-hh.ru`
      и `evilhh.ru` стали бы своими;
    * совпадение суффикса — только в конце имени, поэтому `hh.ru.evil.com`
      чужой;
    * `hh.kz`, `rabota.by` и прочие сайты группы остаются чужими: это
      другие сайты со своими правилами и своей нумерацией вакансий, и
      их id не адресуется как `https://hh.ru/vacancy/{id}`.
    """
    host = url_parts.hostname
    if host is None:
        # Относительная ссылка — свой хост по построению.
        return True
    return host == LISTING_HOST or host.endswith(f".{LISTING_HOST}")


def _canonical_targets(html: str) -> set[str]:
    """На что ссылаются ВСЕ теги `<link rel="canonical">` страницы.

    Три решения, каждое закрывает свою щель.

    1. Поиск ограничен содержимым `<head>`, если он есть. Первое
       совпадение где угодно в документе означало, что фальшивый
       canonical в HTML-комментарии или в JS-строке побеждает настоящий:
       hh.ru отдаёт голову через react-helmet и уже кладёт сериализованное
       состояние в тело страницы.
    2. Возвращаются ВСЕ найденные, а не первый: расхождение между ними —
       само по себе повод не верить странице.
    3. У своего хоста сравнивается путь (у второй страницы в canonical
       ещё и `?page=2`), у чужого — URL целиком: путь `/vacancies/programmist`
       на evil.example.com подтверждал наш листинг, потому что хост не
       смотрели вовсе.
    """
    head = _HEAD_RE.search(html)
    scope = head.group(1) if head is not None else html
    targets: set[str] = set()
    for tag in _CANONICAL_RE.findall(scope):
        href = _HREF_RE.search(tag)
        if href is None:
            continue
        parts = urlsplit(href.group(1))
        if not _is_own_host(parts):
            targets.add(href.group(1))
            continue
        targets.add(parts.path.rstrip("/"))
    return targets


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
    actual = _canonical_targets(html)
    if not actual:
        raise FetchFailed(
            f'на странице листинга {expected} нет тега <link rel="canonical">: '
            "проверить, что hh.ru отдал запрошенный листинг, а не общий индекс, нечем"
        )
    if actual != {expected}:
        raise FetchFailed(
            f"запрошен листинг {expected}, а hh.ru отдал {sorted(actual)!r}. "
            f"Скорее всего, slug {slug!r} не существует — hh.ru не отвечает на такой "
            "404, а молча показывает общий индекс вакансий"
        )


def _item_list(html: str, slug: str) -> list[Any]:
    """Элементы блока JSON-LD с `@type: ItemList`. Дрейф формата — отказ."""
    block = find_ld_json(html, "ItemList")
    if block is None:
        # Причин ровно две, и по одной странице они неразличимы, поэтому
        # называются обе. Живой прогон 2026-07-29 стоил ложной диагностики:
        # отказ объявил смену разметки, а повторный запрос той же страницы
        # разобрался этим же парсером на 20 элементов.
        raise FetchFailed(
            f"на странице листинга {slug!r} нет блока JSON-LD с ItemList. Так выглядит "
            "и смена разметки, и разовый вырожденный ответ hh.ru — отличать по тому, "
            "повторяется ли отказ на других листингах и на следующем прогоне (кэш "
            "условного запроса для этой страницы сброшен, она будет перезапрошена)"
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
    parts = urlsplit(url)
    # Хост обязателен к проверке именно потому, что ниже url собирается
    # заново: без неё ссылка чужого хоста «отмывалась» в
    # https://hh.ru/vacancy/{id} и уходила в базу как настоящая вакансия.
    if not _is_own_host(parts):
        return None, f"ссылка ведёт на чужой хост {parts.netloc!r}: {url!r}"
    vacancy_id = vacancy_id_from_path(parts.path)
    if vacancy_id is None:
        return None, f"url не похож на ссылку на вакансию: {url!r}"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        # Заголовок идёт в скоринг с самым большим весом: пустая строка не
        # «вакансия без названия», а разобранный наполовину элемент, и
        # записывать её значит навсегда зафиксировать нулевой вклад title.
        return None, f"нет непустого заголовка у вакансии {vacancy_id}: {name!r}"
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
    seen: set[str] = set()
    skipped: list[str] = []
    for raw in items:
        vacancy, reason = _parse_item(raw, query_text)
        if vacancy is None:
            skipped.append(reason or "")
            continue
        if vacancy.id in seen:
            # Дубликат — не вторая вакансия. Счётчик `discovered` прогона
            # считается по длине этого списка, и повтор врал бы ему.
            skipped.append(f"вакансия {vacancy.id} уже встречалась на этой странице")
            continue
        seen.add(vacancy.id)
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
