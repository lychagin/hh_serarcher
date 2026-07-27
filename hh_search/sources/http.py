import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Верхний потолок ожидания по Retry-After: сервер не должен уметь подвесить
# процесс на часы/годы, отправив неадекватно большое значение.
MAX_RETRY_AFTER_SEC = 300.0


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
        last_attempt = self._config.max_retries - 1
        for attempt in range(self._config.max_retries):
            self._throttle()
            try:
                response = self._client.get(url, headers=conditional or {})
            except httpx.HTTPError as error:
                last_error = error
                if attempt < last_attempt:
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
            if attempt < last_attempt:
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
        if retry_after:
            parsed = self._parse_retry_after(retry_after)
            if parsed is not None:
                self._sleep(min(parsed, MAX_RETRY_AFTER_SEC))
                return
        self._sleep(self._config.delay_between_requests_sec * (2**attempt))

    def _parse_retry_after(self, value: str) -> float | None:
        """Разбирает Retry-After по RFC 9110: delay-seconds (в т.ч. дробные) или HTTP-date."""
        text = value.strip()
        try:
            return max(float(text), 0.0)
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max((target - datetime.now(UTC)).total_seconds(), 0.0)

    def _check_robots(self, url: str) -> None:
        if not self._config.respect_robots:
            return
        if self._robots is None:
            self._robots = self._load_robots(url)
        if not self._robots.can_fetch(self._user_agent, url):
            raise RobotsDisallowed(f"robots.txt запрещает {url}")

    def _load_robots(self, url: str) -> RobotFileParser:
        """Загружает robots.txt тем же путём, что и обычные запросы (с троттлингом).

        Недоступность (5xx, сетевая ошибка) трактуется как полный запрет по
        умолчанию (RFC 9309 §2.3.1), а 404 — как отсутствие ограничений.
        """
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser = RobotFileParser()
        self._throttle()
        try:
            response = self._client.get(robots_url)
        except httpx.HTTPError as error:
            logger.warning("robots.txt недоступен (%s), доступ запрещён по умолчанию", error)
            raise RobotsDisallowed(
                f"robots.txt недоступен для {robots_url}, доступ запрещён по умолчанию"
            ) from error
        if response.status_code == 404:
            parser.parse([])
            return parser
        if response.status_code != 200:
            logger.warning(
                "robots.txt ответил %s, доступ запрещён по умолчанию", response.status_code
            )
            raise RobotsDisallowed(
                f"robots.txt вернул {response.status_code} для {robots_url}, "
                "доступ запрещён по умолчанию"
            )
        parser.parse(response.text.splitlines())
        return parser
