import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.models import HttpConfig, QuerySpec
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.sources.http import PoliteClient, Robots
from hh_search.sources.listing import build_listing_url
from hh_search.sources.rss import RssQuery, build_rss_url

URL = "https://hh.ru/search/vacancy/rss?text=Yocto"
ROBOTS = "https://hh.ru/robots.txt"
# Живой robots.txt hh.ru, скачанный один раз честным User-Agent. Тесты идут
# на нём, а не на выдуманном: именно живые правила (`Disallow: *?*`,
# `Disallow: /resume$`, `Allow: /vacancies/*?page=`) решают, какой источник
# данных нам вообще доступен.
LIVE_ROBOTS = (Path(__file__).parent / "fixtures" / "robots_hh.txt").read_text(encoding="utf-8")


def make_client(**overrides: object) -> tuple[PoliteClient, list[float]]:
    slept: list[float] = []
    config = HttpConfig(
        delay_between_requests_sec=1.0, timeout_sec=5, max_retries=3, respect_robots=False
    )
    config = config.model_copy(update=overrides)
    return PoliteClient(config, "hh-search/test", sleep=slept.append), slept


@respx.mock
def test_sends_configured_user_agent() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client()
    with client:
        client.get(URL)
    assert route.calls.last.request.headers["User-Agent"] == "hh-search/test"


@respx.mock
def test_throttles_between_requests() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, slept = make_client()
    with client:
        client.get(URL)
        client.get(URL)
    assert any(delay > 0 for delay in slept)


@respx.mock
def test_forbidden_aborts_without_retry() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    client, _ = make_client()
    with client, pytest.raises(AccessForbidden):
        client.get(URL)
    assert route.call_count == 1


@respx.mock
def test_retries_on_429_and_honours_retry_after() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert 7.0 in slept


@respx.mock
def test_gives_up_after_max_retries_on_server_error() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    client, _ = make_client(max_retries=2)
    with client, pytest.raises(FetchFailed):
        client.get(URL)
    assert route.call_count == 2


@respx.mock
def test_passes_conditional_headers_and_returns_304() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    client, _ = make_client()
    with client:
        response = client.get(URL, conditional={"If-None-Match": '"abc"'})
    assert response.status_code == 304
    assert route.calls.last.request.headers["If-None-Match"] == '"abc"'


@respx.mock
def test_respects_robots_disallow() -> None:
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
    )
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(URL)


# --- Раунд исправлений 1 --------------------------------------------------


@respx.mock
def test_robots_fetch_is_throttled_like_regular_requests() -> None:
    """Находка 1 (Critical): запрос robots.txt должен участвовать в троттлинге,
    то есть между ним и первым реальным запросом обязана быть пауза."""
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /forbidden/\n")
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, slept = make_client(respect_robots=True, delay_between_requests_sec=0.5)
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert slept, "ожидалась пауза между robots.txt и основным запросом"
    assert slept[0] == pytest.approx(0.5, abs=0.1)


@respx.mock
def test_robots_404_means_no_restrictions() -> None:
    """Находка 3 (Important): 404 у robots.txt — штатная ситуация, доступ разрешён."""
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client:
        response = client.get(URL)
    assert response.status_code == 200


@respx.mock
def test_robots_5xx_means_disallowed_by_default() -> None:
    """Находка 3 (Important): недоступный robots.txt (5xx) — запрет по умолчанию."""
    respx.get(ROBOTS).mock(return_value=httpx.Response(500))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(URL)


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_robots_401_and_403_are_a_block_and_not_a_rule(status: int) -> None:
    """Отказ ДОСТУПА к самому `robots.txt` — это блокировка, а не правило.

    Файл отдаётся анонимно всем, поэтому 403 (и 401) именно на нём означает
    «закрыли нас». Сообщение это говорило и раньше, а тип исключения — нет,
    и предохранители проходили мимо: `ForbiddenStreak` считает только
    `AccessForbidden`, поэтому маркер `access-forbidden.stop` не ставился
    НИКОГДА, а демон стучался каждые четыре часа вечно.

    5xx остаётся `RobotsDisallowed` (см. тест выше) сознательно: там
    источник сломан, а не закрыт для нас, и останавливать демон навсегда
    из-за чужой аварии нельзя.
    """
    respx.get(ROBOTS).mock(return_value=httpx.Response(status))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(AccessForbidden, match="блокировку источника"):
        client.get(URL)


@respx.mock
def test_robots_network_error_means_disallowed_by_default() -> None:
    """Находка 3 (Important): сетевая ошибка при получении robots.txt — запрет."""
    respx.get(ROBOTS).mock(side_effect=httpx.ConnectError("boom"))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(URL)


@respx.mock
def test_retry_after_accepts_http_date() -> None:
    """Находка 2 (Important): Retry-After в формате HTTP-date (RFC 9110)."""
    target = datetime.now(UTC) + timedelta(seconds=5)
    retry_after = format_datetime(target, usegmt=True)
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": retry_after}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    # Полуинтервал, а не «около пяти»: `format_datetime` округляет дату
    # ВНИЗ до целой секунды, поэтому ожидание — это (4.0, 5.0], причём
    # ровно 4.0 достижимо и наблюдалось (прогон 2026-07-28 упал на
    # `abs(4.0 - 5.0) < 1.0`). Различающая сила от этого не страдает:
    # запасной путь (Retry-After не разобран) дал бы 1.0 — паузу между
    # запросами, умноженную на 2**0.
    assert any(4.0 <= delay <= 5.0 for delay in slept)


@respx.mock
def test_retry_after_accepts_fractional_seconds() -> None:
    """Находка 2 (Important): дробные секунды в Retry-After не отбрасываются."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert 2.5 in slept


@respx.mock
def test_retry_after_is_capped() -> None:
    """Находка 4 (Minor): неадекватно большой Retry-After ограничивается потолком."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "99999999"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert 300.0 in slept
    assert 99999999.0 not in slept


@respx.mock
def test_no_sleep_after_final_failed_attempt() -> None:
    """Находка 5 (Minor): после последней неудачной попытки бэкофф не нужен —
    экспоненциальный бэкофф после финальной попытки (2**(max_retries-1) * delay
    = 2.0 в этой конфигурации) не должен встречаться."""
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    client, slept = make_client(max_retries=2)
    with client, pytest.raises(FetchFailed):
        client.get(URL)
    assert route.call_count == 2
    assert all(delay < 2.0 for delay in slept)


# --- Раунд исправлений 2 ---------------------------------------------------


@respx.mock
def test_retry_after_nan_falls_back_to_normal_backoff() -> None:
    """Находка раунда 2 (Important): float("nan") не бросает ValueError, а любое
    сравнение с NaN ложно, поэтому на ecd05dd nan проходил сквозь min() в
    _backoff() неизменным и улетал в sleep() как есть (в проде с настоящим
    time.sleep это аварийно валит процесс). Мусорное значение обязано приводить
    к обычному экспоненциальному backoff, как и для любого другого невалидного
    заголовка."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "nan"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert not any(math.isnan(delay) for delay in slept)
    assert 1.0 in slept  # обычный backoff: delay_between_requests_sec * 2**0


@respx.mock
def test_retry_after_infinite_is_capped_at_300() -> None:
    """Находка раунда 2: Retry-After: inf уже был безопасен (min(inf, 300.0) даёт
    потолок корректно) — фиксируем это поведение отдельным тестом."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "inf"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert 300.0 in slept


# --- Раунд исправлений 3: robots.txt на живых правилах hh.ru ----------------
#
# Общая причина всех тестов ниже: urllib.robotparser не поддерживает
# подстановочные знаки (RuleLine.applies_to == startswith по quote()-нутому
# пути) и сравнивает только путь без query. На 765a1ba это делало живые
# запреты hh.ru невидимыми, а respect_robots: true — декларацией.


def live_robots_response() -> httpx.Response:
    return httpx.Response(200, text=LIVE_ROBOTS, headers={"Content-Type": "text/plain"})


@respx.mock
def test_live_robots_forbids_rss_search() -> None:
    """`Disallow: *?*` в секции User-agent: * запрещает ЛЮБОЙ URL с query-строкой,
    то есть весь RSS-поиск. На 765a1ba шаблон превращался в '%2A%3F%2A' и не
    совпадал ни с чем — запрос уходил на hh.ru как разрешённый."""
    respx.get(ROBOTS).mock(return_value=live_robots_response())
    rss = "https://hh.ru/search/vacancy/rss?text=Yocto&order_by=publication_time"
    route = respx.get(rss).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(rss)
    assert route.call_count == 0, "запрещённый URL не должен уходить в сеть"


@respx.mock
def test_live_robots_forbids_resume_by_dollar_anchor() -> None:
    """`Disallow: /resume$` — якорь конца пути. stdlib его тоже не понимала."""
    respx.get(ROBOTS).mock(return_value=live_robots_response())
    respx.get("https://hh.ru/resume").mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get("https://hh.ru/resume")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://hh.ru/vacancy/135586311", "страница вакансии: query нет, запрет *?* не бьёт"),
        ("https://hh.ru/vacancies/programmist", "листинг без query — под запрет не попадает"),
        (
            "https://hh.ru/vacancies/programmist?page=2",
            "Allow: /vacancies/*?page= (18 символов) длиннее Disallow: *?* (3) и побеждает",
        ),
        ("https://hh.ru/resumelike", "якорь $ не даёт /resume$ съесть более длинный путь"),
    ],
)
@respx.mock
def test_live_robots_allows_listing_and_vacancy_pages(url: str, reason: str) -> None:
    """Разрешённые живыми правилами URL обязаны проходить.

    Эти четыре случая — не дифференциальные (на 765a1ba матчер разрешал всё
    подряд, поэтому они там зелёные). Их работа — сторожить, чтобы новый матчер
    не свалился в противоположную крайность: на них строится переезд discovery
    с RSS на листинг /vacancies/{slug} + ?page=N."""
    respx.get(ROBOTS).mock(return_value=live_robots_response())
    route = respx.get(url).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client:
        response = client.get(url)
    assert response.status_code == 200, reason
    assert route.call_count == 1


@respx.mock
def test_redirect_to_another_host_rechecks_that_hosts_robots() -> None:
    """hh.ru молча уводит региональные вакансии (Астана, Алматы, Минск) на
    hh.kz/rabota.by. На 765a1ba follow_redirects=True проходил чужой хост
    вообще без обращения к его robots.txt, а кэш из одного объекта подсовывал
    правила hh.ru."""
    respx.get(ROBOTS).mock(return_value=live_robots_response())
    foreign_robots = respx.get("https://hh.kz/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /\n", headers={"Content-Type": "text/plain"}
        )
    )
    respx.get("https://hh.ru/vacancy/1").mock(
        return_value=httpx.Response(302, headers={"Location": "https://hh.kz/vacancy/1"})
    )
    target = respx.get("https://hh.kz/vacancy/1").mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get("https://hh.ru/vacancy/1")
    assert foreign_robots.call_count == 1, "robots.txt нового хоста обязан запрашиваться"
    assert target.call_count == 0, "запрещённая чужим robots.txt страница не качается"


@respx.mock
def test_every_redirect_hop_is_throttled() -> None:
    """Пауза между запросами обязана соблюдаться и для редиректов. На 765a1ba
    httpx проходил цепочку внутри одного вызова, мимо _throttle: два запроса к
    hh.ru уходили подряд без паузы."""
    respx.get("https://hh.ru/vacancy/1").mock(
        return_value=httpx.Response(301, headers={"Location": "https://hh.ru/vacancy/2"})
    )
    respx.get("https://hh.ru/vacancy/2").mock(return_value=httpx.Response(200, text="ok"))
    client, slept = make_client(respect_robots=False, delay_between_requests_sec=0.5)
    with client:
        response = client.get("https://hh.ru/vacancy/1")
    assert response.status_code == 200
    assert slept, "между хопом редиректа и следующим запросом обязана быть пауза"
    assert slept[0] == pytest.approx(0.5, abs=0.1)


@respx.mock
def test_redirect_loop_is_bounded() -> None:
    """Кольцо редиректов не должно крутиться вечно."""
    respx.get("https://hh.ru/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://hh.ru/a"})
    )
    client, _ = make_client(respect_robots=False)
    with client, pytest.raises(FetchFailed):
        client.get("https://hh.ru/a")


@respx.mock
def test_robots_200_with_html_body_means_disallowed() -> None:
    """Заглушка антибота, отданная со статусом 200, разбиралась в «правил нет»
    и давала fail-open. Непонятное тело трактуется как недоступность."""
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(
            200,
            text="<!DOCTYPE html><html><body>Проверка браузера</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(URL)
    assert route.call_count == 0


@respx.mock
def test_robots_cache_does_not_leak_between_hosts() -> None:
    """Кэш robots — по origin, не один объект на клиента. На 765a1ba правила
    первого хоста применялись ко всем последующим."""
    ru_robots = respx.get(ROBOTS).mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /kz/\n", headers={"Content-Type": "text/plain"}
        )
    )
    kz_robots = respx.get("https://hh.kz/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /vacancy/\n", headers={"Content-Type": "text/plain"}
        )
    )
    respx.get("https://hh.ru/vacancy/1").mock(return_value=httpx.Response(200, text="ok"))
    respx.get("https://hh.ru/vacancy/2").mock(return_value=httpx.Response(200, text="ok"))
    respx.get("https://hh.kz/vacancy/1").mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client:
        assert client.get("https://hh.ru/vacancy/1").status_code == 200
        assert client.get("https://hh.ru/vacancy/2").status_code == 200
        with pytest.raises(RobotsDisallowed):
            client.get("https://hh.kz/vacancy/1")
    assert ru_robots.call_count == 1, "robots одного хоста качается ровно один раз"
    assert kz_robots.call_count == 1, "у нового хоста спрашивается его собственный robots"


@respx.mock
def test_group_for_our_user_agent_wins_over_star() -> None:
    """Группа выбирается по токену продукта регистронезависимо; несколько строк
    User-agent подряд задают одну группу."""
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(
            200,
            text=(
                "User-agent: *\n"
                "Disallow: /\n"
                "\n"
                "User-agent: SomeoneElse\n"
                "User-agent: HH-Search\n"
                "Disallow: /private/\n"
            ),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get("https://hh.ru/vacancy/1").mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client(respect_robots=True)
    with client:
        assert client.get("https://hh.ru/vacancy/1").status_code == 200
        with pytest.raises(RobotsDisallowed):
            client.get("https://hh.ru/private/x")


@respx.mock
def test_get_after_close_fails_as_fetch_failed() -> None:
    """Закрытый клиент — штатный отказ, а не RuntimeError мимо иерархии ошибок.

    Прогон ловит FetchFailed и продолжается частично; RuntimeError уронил бы его
    целиком, потеряв уже собранные вакансии.
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client()
    with client:
        client.get(URL)
    with pytest.raises(FetchFailed):
        client.get(URL)


# --- Раунд переезда discovery на листинг ---------------------------------


@respx.mock
def test_polite_client_passes_urls_built_for_discovery_and_still_blocks_rss() -> None:
    """Сквозная проверка переезда: не «какой-то листинговый URL проходит»,
    а именно тот, что строит `build_listing_url`, — обе его формы. Тест
    сторожит связку целиком, поэтому попытка вернуть фильтрацию через
    query-параметры (`?area=66&page=1`) покраснеет здесь, а не на проде.
    Контроль в том же тесте: старый RSS-URL по-прежнему не проходит."""
    respx.get(ROBOTS).mock(return_value=live_robots_response())
    query = QuerySpec(slug="programmist", cluster="embedded")
    first_page = build_listing_url(query)
    second_page = build_listing_url(query, page=1)
    assert first_page == "https://hh.ru/vacancies/programmist"
    assert second_page == "https://hh.ru/vacancies/programmist?page=1"

    client, _ = make_client(respect_robots=True)
    with client:
        for url in (first_page, second_page):
            respx.get(url).mock(return_value=httpx.Response(200, text="ok"))
            assert client.get(url).status_code == 200, f"discovery обязан проходить: {url}"

        rss = build_rss_url(RssQuery(text="Yocto"))
        route = respx.get(rss).mock(return_value=httpx.Response(200, text="ok"))
        with pytest.raises(RobotsDisallowed):
            client.get(rss)
        assert route.call_count == 0, "RSS запрещён `Disallow: *?*` и в сеть не уходит"


# --- Раунд исправлений 6: проверяем ровно тот URL, который уйдёт в сеть ------
#
# Матчер разбирал URL-строку как есть, а httpx перед отправкой нормализует
# путь по RFC 3986 и схлопывает dot-сегменты. Проверка robots и фактический
# запрос расходились ПО ПОСТРОЕНИЮ, и любая точка входа в URL наследовала
# эту щель.


def counting_transport(sent: list[str]) -> httpx.MockTransport:
    """Мок-транспорт, записывающий URL в том виде, в каком его отправил httpx."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=LIVE_ROBOTS, headers={"Content-Type": "text/plain"})
        sent.append(str(request.url))
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


def robots_aware_client(sent: list[str]) -> PoliteClient:
    config = HttpConfig(
        delay_between_requests_sec=0.1, timeout_sec=5, max_retries=3, respect_robots=True
    )
    return PoliteClient(
        config, "hh-search/test", sleep=lambda _: None, transport=counting_transport(sent)
    )


@pytest.mark.parametrize(
    ("requested", "normalized_by_httpx"),
    [
        ("https://hh.ru/vacancies/.?page=1", "https://hh.ru/vacancies?page=1"),
        ("https://hh.ru/vacancies/..?page=1", "https://hh.ru?page=1"),
        (
            "https://hh.ru/vacancies/programmist/../search/vacancy/rss?text=Yocto",
            "https://hh.ru/vacancies/search/vacancy/rss?text=Yocto",
        ),
    ],
)
def test_robots_verdict_is_taken_on_the_url_that_actually_goes_out(
    requested: str, normalized_by_httpx: str
) -> None:
    """Проверяется не «что мы проверили», а «что реально ушло в сеть».

    Сначала фиксируем саму щель: httpx схлопывает dot-сегменты, поэтому
    отправляемый URL отличается от запрошенного. Затем требуем, чтобы
    вердикт брался по отправляемому — все три нормализованных URL живые
    правила hh.ru ЗАПРЕЩАЮТ (`Disallow: *?*`), и счётчик запросов обязан
    остаться нулевым.
    """
    assert str(httpx.URL(requested)) == normalized_by_httpx, "щель между проверкой и отправкой"
    assert not Robots.parse(LIVE_ROBOTS).can_fetch("hh-search/test", normalized_by_httpx)

    sent: list[str] = []
    client = robots_aware_client(sent)
    with client, pytest.raises(RobotsDisallowed):
        client.get(requested)
    assert sent == [], f"запрещённый {normalized_by_httpx} не имеет права уходить в сеть"


def test_normalization_does_not_start_forbidding_the_allowed_urls() -> None:
    """Контроль к предыдущему тесту: нормализация не должна свалиться в
    противоположную крайность и запретить обе разрешённые формы листинга."""
    sent: list[str] = []
    client = robots_aware_client(sent)
    query = QuerySpec(slug="programmist", cluster="embedded")
    with client:
        for url in (build_listing_url(query), build_listing_url(query, page=1)):
            assert client.get(url).status_code == 200
    assert sent == [
        "https://hh.ru/vacancies/programmist",
        "https://hh.ru/vacancies/programmist?page=1",
    ]


# --- Раунд исправлений 8: отказ источника не имеет права УСИЛИВАТЬ нагрузку --
#
# Отрицательный вердикт `robots.txt` не кэшировался вовсе: `self._robots`
# заполнялся только при успехе, поэтому каждая несделанная попытка стоила
# нового запроса к `/robots.txt`. Замер прогона при 403: 16 запросов, все
# 16 — к `/robots.txt`, при девяти страницах листингов и семи вакансиях в
# очереди. На первом дне с очередью в 98 это 107 запросов за прогон и 642
# в сутки — к источнику, который нам только что отказал.


def counting_robots_transport(statuses: list[int], seen: list[str]) -> httpx.MockTransport:
    """Транспорт, отвечающий на `/robots.txt` заданными кодами по очереди."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(statuses.pop(0) if statuses else 200)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


def denied_client(statuses: list[int], seen: list[str]) -> PoliteClient:
    config = HttpConfig(
        delay_between_requests_sec=0.1, timeout_sec=5, max_retries=3, respect_robots=True
    )
    return PoliteClient(
        config,
        "hh-search/test",
        sleep=lambda _: None,
        transport=counting_robots_transport(statuses, seen),
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AccessForbidden), (500, RobotsDisallowed)],
)
def test_a_denied_robots_is_asked_once_per_origin_and_not_once_per_attempt(
    status: int, error: type[Exception]
) -> None:
    """Один запрос на origin за прогон, а не один на каждую попытку.

    Считается фактическое число запросов, а не число отказов: смысл
    исправления именно в том, чтобы источник, который нам отказал, не
    получил ещё двадцать запросов подряд.
    """
    seen: list[str] = []
    client = denied_client([status] * 20, seen)
    with client:
        for number in range(20):
            with pytest.raises(error):
                client.get(f"https://hh.ru/vacancy/{number}")
    assert seen == ["https://hh.ru/robots.txt"]


def test_the_cached_denial_keeps_its_type_on_every_attempt() -> None:
    """Тип отказа обязан пережить кэш: иначе предохранитель считает первый
    403 и не считает остальные, а срабатывает он на ВТОРОЙ подряд."""
    seen: list[str] = []
    client = denied_client([403], seen)
    with client:
        for number in range(3):
            with pytest.raises(AccessForbidden, match="блокировку источника"):
                client.get(f"https://hh.ru/vacancy/{number}")
    assert len(seen) == 1


def test_the_denial_is_cached_per_origin_and_not_for_the_whole_client() -> None:
    """Запрет hh.ru не имеет права молча распространяться на hh.kz.

    Кэш положительных правил уже по origin (спека §3.5); отрицательный
    обязан быть таким же, иначе редирект на региональный домен получал бы
    вердикт чужого хоста.
    """
    seen: list[str] = []
    client = denied_client([403, 200], seen)
    with client:
        with pytest.raises(AccessForbidden):
            client.get("https://hh.ru/vacancy/1")
        assert client.get("https://hh.kz/vacancy/1").status_code == 200
    assert seen == [
        "https://hh.ru/robots.txt",
        "https://hh.kz/robots.txt",
        "https://hh.kz/vacancy/1",
    ]


def test_the_denial_does_not_outlive_the_client() -> None:
    """Кэш живёт ровно прогон: временный сбой не блокирует нас навсегда.

    Осознанное решение прошлых раундов — `PoliteClient` создаётся на
    каждый прогон, поэтому следующий прогон обязан спросить `robots.txt`
    заново, даже если предыдущему отказали.
    """
    seen: list[str] = []
    statuses = [500, 200]
    first = denied_client(statuses, seen)
    with first, pytest.raises(RobotsDisallowed):
        first.get("https://hh.ru/vacancy/1")

    second = denied_client(statuses, seen)
    with second:
        assert second.get("https://hh.ru/vacancy/1").status_code == 200
    assert seen.count("https://hh.ru/robots.txt") == 2
