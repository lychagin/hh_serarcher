import logging
import time
from collections.abc import Callable
from types import TracebackType
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class PoliteClient:
    """HTTP-клиент, соблюдающий robots.txt, паузы между запросами и Retry-After."""

    def __init__(
        self,
        config: HttpConfig,
        user_agent: str,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._user_agent = user_agent
        self._sleep = sleep
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=config.timeout_sec,
            follow_redirects=True,
            transport=transport,
        )
        self._last_request_at: float | None = None
        self._robots: RobotFileParser | None = None

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, conditional: dict[str, str] | None = None) -> httpx.Response:
        self._check_robots(url)
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            self._throttle()
            try:
                response = self._client.get(url, headers=conditional or {})
            except httpx.HTTPError as error:
                last_error = error
                self._backoff(attempt)
                continue

            if response.status_code == 403:
                raise AccessForbidden(
                    f"hh.ru ответил 403 на {url}. Возможно, источник закрыли. "
                    "Прогон остановлен, обходные пути не применяются."
                )
            if response.status_code not in RETRYABLE_STATUSES:
                return response

            last_error = FetchFailed(f"{response.status_code} на {url}")
            self._backoff(attempt, response.headers.get("Retry-After"))

        raise FetchFailed(f"не удалось получить {url}: {last_error}")

    def _throttle(self) -> None:
        if self._last_request_at is None:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._config.delay_between_requests_sec - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after and retry_after.strip().isdigit():
            self._sleep(float(retry_after.strip()))
            return
        self._sleep(self._config.delay_between_requests_sec * (2**attempt))

    def _check_robots(self, url: str) -> None:
        if not self._config.respect_robots:
            return
        if self._robots is None:
            self._robots = self._load_robots(url)
        if not self._robots.can_fetch(self._user_agent, url):
            raise RobotsDisallowed(f"robots.txt запрещает {url}")

    def _load_robots(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        parser = RobotFileParser()
        try:
            response = self._client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
        except httpx.HTTPError:
            logger.warning("robots.txt недоступен, считаем что ограничений нет")
            parser.parse([])
        return parser
