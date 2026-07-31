"""Канарейка на смену формата источника. Ходит в живой hh.ru.

В CI пропускается настройкой `addopts = "-m 'not network'"` из Task 1,
запускается руками: `pytest -m network`.

Четыре решения, каждое — следствие того, что тест ходит наружу.

1. **Проверяется листинг и страница вакансии, а не RSS.** RSS-поиск
   запрещён живым `robots.txt` (`Disallow: *?*`, спека §3.2), discovery
   работает через `/vacancies/{slug}`. Канарейка обязана сторожить тот
   источник, который используется.
2. **Ходим клиентом проекта, а не `httpx` напрямую.** Тогда тест
   спрашивает `robots.txt`, выдерживает паузу и представляется тем же
   `User-Agent` с контактным адресом, что и прод (спека §3.5). Тест,
   который ведёт себя невежливее сервиса, — способ получить бан на ровном
   месте. Заодно проверяется, что запрещённый URL мы бы и не построили:
   `build_listing_url` и `vacancy_url` — функции проекта, а не строки,
   склеенные здесь.
3. **Недоступность источника — это `skip`, а не `fail`.** Красный тест
   обязан значить «источник сменил формат»; `httpx.ConnectError` наружу
   лишает канарейку смысла. Связность проверяется TCP-соединением, без
   единого HTTP-запроса к hh.ru, а транспортный отказ уже в процессе
   (`FetchFailed` — таймаут, 5xx) пропускает тест по той же причине.
   `403` и запрет в `robots.txt`, наоборот, именно `fail`: это
   блокировка и отзыв разрешения, и узнать о них надо громко.
4. **Единицы запросов, с паузой между ними**: `robots.txt` каждого
   origin, ОДНА страница листинга, ОДНА страница вакансии и ОДНА вторая
   страница листинга (`?page=1`) — та проверяет, что `Allow:
   /vacancies/*?page=` жив, и без неё проверка запрета проходила бы
   вхолостую при fail-closed. Id вакансии берётся из первого ответа, а
   не зашивается в тест. hh.ru отвечает на
   `/vacancies/{slug}` редиректом 302 на региональный поддомен, поэтому
   `robots.txt` честно спрашивается и у поддомена (кэш правил — по
   origin). Этот путь тест проходит целиком: если «свой хост» когда-нибудь
   снова сузится до одного `hh.ru`, красным станет разбор листинга.
"""

import re
import socket
from collections.abc import Iterator

import pytest

from hh_search.config.models import HttpConfig, QuerySpec, WorkFormat
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import build_listing_url, parse_listing
from hh_search.sources.rss import RssQuery, build_rss_url
from hh_search.sources.vacancy_page import find_ld_json, vacancy_url

CONTACT = "serg.lychagin.usa@gmail.com"
# Тот же User-Agent, что у прода: контактный адрес — способ hh.ru с нами
# связаться, и тест не имеет права представляться скромнее сервиса.
USER_AGENT = f"hh-search/0.1 (personal job search; {CONTACT})"
QUERY = QuerySpec(slug="programmist", cluster="backend")
# Второй поток — тот же листинг с фильтром `work_format`. Форма URL и
# хрупкое место (`canonical` без `work_format`) описаны у `QUERY_REMOTE`
# ниже, рядом с тестами, которые их проверяют.
QUERY_REMOTE = QuerySpec(slug="programmist", cluster="remote", work_format=WorkFormat.REMOTE)
# Ключ встроенного состояния листинга, несущий формат работы конкретной
# вакансии, — та же обёртка, что и `_WORK_FORMATS_RE` в `sources/work_format.py`
# (там она читает страницу ОДНОЙ вакансии и требует согласия всех вхождений;
# здесь вакансий в выдаче много, согласия не будет, и это законно — ищем
# просто ФАКТ присутствия REMOTE хотя бы в одной). Значения приходят
# HTML-экранированными, потому что блок вставлен как значение атрибута.
_WORK_FORMATS_RE = re.compile(r"&#34;workFormatsElement&#34;:\[([^\]]*)\]")

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def client() -> Iterator[PoliteClient]:
    try:
        socket.create_connection(("hh.ru", 443), timeout=5).close()
    except OSError as error:
        pytest.skip(f"нет сети до hh.ru ({error}) — это не смена формата источника")
    # Пауза вдвое больше рабочей, повторов — минимум: канарейка не имеет
    # права стоить источнику больше, чем сам сервис.
    config = HttpConfig(delay_between_requests_sec=2.0, max_retries=2)
    with PoliteClient(config, USER_AGENT) as polite:
        yield polite


def _fetch(client: PoliteClient, url: str, what: str) -> str:
    """Один GET со всей вежливостью проекта и с честным разбором отказа."""
    try:
        response = client.get(url)
    except AccessForbidden as error:
        pytest.fail(f"hh.ru закрыл доступ: {error}")
    except RobotsDisallowed as error:
        pytest.fail(f"robots.txt больше не разрешает {what}: {error}")
    except FetchFailed as error:
        pytest.skip(f"{what} недоступен ({error}) — это не смена формата источника")
    assert response.status_code == 200, f"{what} отдал {response.status_code}"
    return response.text


@pytest.fixture(scope="module")
def listing_html(client: PoliteClient) -> str:
    return _fetch(client, build_listing_url(QUERY), f"листинг /vacancies/{QUERY.slug}")


def test_listing_still_exposes_item_list(listing_html: str) -> None:
    """`ItemList` в ld+json — единственный источник id для всего конвейера."""
    item_list = find_ld_json(listing_html, "ItemList")
    assert item_list is not None, "на странице листинга пропал ld+json ItemList — см. §3.2"
    assert isinstance(item_list.get("itemListElement"), list)


def test_listing_items_still_parse_into_vacancies(listing_html: str) -> None:
    """Заодно сторож редиректа на региональный поддомен: и `canonical`, и все
    двадцать ссылок приходят с поддомена, и сужение «своего хоста» до одного
    `hh.ru` красит именно этот тест."""
    vacancies = parse_listing(listing_html, QUERY.slug)
    assert vacancies, "ни один элемент листинга не разобрался — см. §3.2"
    assert all(item.url.startswith("https://hh.ru/vacancy/") for item in vacancies)
    assert all(item.id.isdigit() and item.title for item in vacancies)


def test_vacancy_page_still_exposes_job_posting(client: PoliteClient, listing_html: str) -> None:
    """`JobPosting.description` — единственное, за чем мы идём на страницу."""
    vacancy = parse_listing(listing_html, QUERY.slug)[0]
    page = _fetch(client, vacancy_url(vacancy.id), f"страница вакансии {vacancy.id}")
    posting = find_ld_json(page, "JobPosting")
    assert posting is not None, "JSON-LD JobPosting пропал — см. §3.4"
    description = posting.get("description")
    assert isinstance(description, str) and description.strip(), "description пуст или не строка"
    # Проверки зарплаты здесь НЕТ, и это решение, а не пропуск. Она тут была,
    # записанная как `'data-qa="vacancy-salary' in page or extract_salary(page)
    # is None`, и была тавтологией: `extract_salary` ищет ровно этот атрибут,
    # поэтому при его исчезновении второе слагаемое истинно — утверждение
    # зелено и при переименовании атрибута, то есть ровно в том случае, ради
    # которого писалось. Починить его выборкой из одной страницы нельзя: «блок
    # зарплаты не найден» — законный результат для конкретной вакансии (§3.4),
    # и отличить его от дрейфа можно только агрегатом. Этим и занят
    # `SalaryBlockStats` на настоящем прогоне («ни на одной странице»), а
    # канарейка честно не обещает того, чего одна страница дать не может.


@pytest.fixture(scope="module")
def remote_listing_html(client: PoliteClient) -> str:
    """Второй поток (`work_format=REMOTE`) не имел живого контрактного
    покрытия вовсе: `work_format` в этом файле не встречался ни разу.

    Успешный `_fetch` уже доказывает первый пункт контракта — что форма
    URL остаётся разрешённой живым `robots.txt`: `client.get` сверяет URL
    с правилами ДО запроса и поднял бы `RobotsDisallowed` (а `_fetch` —
    `pytest.fail`), если бы `Allow: /vacancies/*?*&page=` исчезло или
    `work_format` стало запрещённым отдельно от него.
    """
    url = build_listing_url(QUERY_REMOTE)
    assert "&page=" in url, "без &page= второй поток попал бы под Disallow: *?* — см. §1"
    return _fetch(client, url, f"второй поток /vacancies/{QUERY_REMOTE.slug} (work_format=REMOTE)")


def test_remote_stream_listing_still_parses_despite_canonical_without_work_format(
    remote_listing_html: str,
) -> None:
    """Самое хрупкое место второго потока (замер 2026-07-31, фикстура
    `tests/fixtures/listing_remote_programmist.html.gz`): hh.ru не отражает
    `work_format` в `<link rel="canonical">` второго потока. `_check_slug`
    это переживает не потому, что знает про `work_format`, а потому что
    `_canonical_targets` сравнивает `path`, а не URL целиком, — то же
    решение, что уже держит `?page=N` первого потока. Если сравнение
    когда-нибудь станет строже (или canonical начнёт нести `work_format` в
    ИНОЙ форме, ломающей путь), разбор второго потока упадёт именно здесь.

    Числа вакансий и регионов здесь намеренно не проверяются: живая выдача
    меняется каждый день, а контрактный тест обязан ловить смену контракта,
    а не движение данных.
    """
    vacancies = parse_listing(remote_listing_html, QUERY_REMOTE.slug)
    assert vacancies, "второй поток не разобрался ни в одну вакансию — см. §1 спеки региона"
    assert all(item.url.startswith("https://hh.ru/vacancy/") for item in vacancies)
    assert all(item.id.isdigit() and item.title for item in vacancies)


def test_remote_stream_state_still_carries_the_remote_format(remote_listing_html: str) -> None:
    """Фильтр не выродился в пустышку: хотя бы один блок формата работы во
    встроенном состоянии страницы несёт REMOTE. Доля и число таких блоков
    здесь не утверждаются — это тоже движение данных, а не контракт;
    достаточно факта, что фильтр действительно на что-то влияет."""
    blocks = _WORK_FORMATS_RE.findall(remote_listing_html)
    assert blocks, "во встроенном состоянии не нашлось ни одного блока формата работы"
    assert any("REMOTE" in block for block in blocks), (
        "ни один блок формата работы не содержит REMOTE — фильтр work_format=REMOTE "
        "перестал на что-либо влиять"
    )


def test_live_robots_still_permits_the_source_we_chose(client: PoliteClient) -> None:
    """Самый дорогой факт проекта — и потому единственный, чья проверка обязана быть.

    На `Disallow: *?*` стоит выбор источника данных для всего сервиса: из-за
    него discovery переехал с RSS-поиска на листинг (§0, §3.2). Уйдёт правило —
    RSS вернётся источником на порядок богаче; появится запрет на
    `/vacancies/*?page=` — рассыплется пагинация, то есть единственный способ
    расширить окно выдачи. Ни то ни другое нельзя узнать из фикстуры: она
    сторожит РАЗБОР живых правил (`tests/test_http.py`), а живые правила
    сторожит только этот тест.

    Оба утверждения нужны вместе, и по построению: при недоступном или
    непонятном `robots.txt` клиент запрещает всё (fail-closed, §3.5), и
    одинокая проверка запрета проходила бы вхолостую. Разрешение проверяется
    на том же URL, которым ходит прод (`build_listing_url(..., page=1)`), —
    то есть заодно на том, что мы не научились строить запрещённый URL.

    Стоимость — один запрос: правила origin к этому моменту уже в кэше
    клиента, а запрещённый URL отвергается до выхода в сеть.
    """
    with pytest.raises(RobotsDisallowed):
        client.get(build_rss_url(RssQuery(text="Yocto")))
    second_page = build_listing_url(QUERY, page=1)
    assert "?page=1" in second_page, (
        "вторая страница обязана нести query-строку, иначе нечего проверять"
    )
    assert _fetch(client, second_page, f"вторая страница листинга /vacancies/{QUERY.slug}")
