import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.sources.http import PoliteClient

URL = "https://hh.ru/search/vacancy/rss?text=Yocto"
ROBOTS = "https://hh.ru/robots.txt"


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
    assert any(abs(delay - 5.0) < 1.0 for delay in slept)


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
