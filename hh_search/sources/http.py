import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Редиректы обходим вручную, чтобы проверить robots.txt и выдержать паузу на
# КАЖДОМ хопе; потолок нужен, чтобы кольцо редиректов не крутилось вечно.
MAX_REDIRECTS = 5
# Верхний потолок ожидания по Retry-After: сервер не должен уметь подвесить
# процесс на часы/годы, отправив неадекватно большое значение.
MAX_RETRY_AFTER_SEC = 300.0


@dataclass(frozen=True)
class _Rule:
    """Одно правило Allow/Disallow: исходный шаблон и его скомпилированный вид."""

    allow: bool
    pattern: str
    matcher: re.Pattern[str]


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Компилирует шаблон пути robots.txt в регулярное выражение (RFC 9309 §2.2.3).

    `*` — любая последовательность символов, `$` в конце — якорь конца пути.
    Начало пути заякорено неявно: сопоставление всегда идёт через re.match.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def _product_tokens(user_agent: str) -> set[str]:
    """Достаёт токены продукта из заголовка User-Agent (RFC 9309 §2.2.1).

    "hh-search/0.1 (contact: a@b)" -> {"hh-search", "hh-search/0.1 (contact: a@b)", ...}.
    """
    tokens = {user_agent.strip().lower()}
    for piece in user_agent.lower().split():
        token = piece.split("/", 1)[0].strip("()[],;")
        if token:
            tokens.add(token)
    return tokens


def normalize(url: str) -> str:
    """URL в том виде, в каком его отправит httpx (RFC 3986 §6.2).

    Единственная точка нормализации в модуле, и вызывается она ДО проверки
    robots.txt. Причина — расхождение по построению: `httpx` перед
    отправкой схлопывает dot-сегменты, поэтому проверка строки «как она
    пришла» проверяла не тот URL, который уходит в сеть.
    `/vacancies/.?page=1` превращался в `/vacancies?page=1`, а
    `/vacancies/..?page=1` — в пустой путь, то есть `/?page=1` для
    сопоставления (см. `_target_path`); оба запрещены живым правилом
    hh.ru `Disallow: *?*`, тогда как проверенная форма попадала под
    `Allow: /vacancies/*?page=` и проходила. Нормализуем сами и дальше
    работаем ровно с этой строкой — и матчер, и сам запрос.
    """
    return str(httpx.URL(url))


def _target_path(url: str) -> str:
    """Путь, по которому сопоставляются правила: путь ВМЕСТЕ с query-строкой.

    Именно это упускала urllib.robotparser: она сравнивала только путь, да ещё и
    через quote(), поэтому правило hh.ru `Disallow: *?*` было для неё невидимым.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


class Robots:
    """Матчер robots.txt по RFC 9309.

    Написан вручную, потому что urllib.robotparser не поддерживает подстановки:
    её RuleLine.applies_to — это startswith() по quote()-нутому пути. На живом
    robots.txt hh.ru это означало, что `Disallow: *?*` (запрет любого URL с
    query-строкой) и `Disallow: /resume$` просто не применялись, а
    respect_robots: true давал ложную уверенность.
    """

    def __init__(self, groups: dict[str, list[_Rule]]) -> None:
        self._groups = groups

    @classmethod
    def parse(cls, text: str) -> "Robots":
        """Разбирает текст robots.txt в группы правил по токену User-agent.

        Несколько строк `User-agent` подряд задают одну группу; повторная группа
        для того же токена дополняет уже накопленные правила. Пустое значение
        Allow/Disallow игнорируется, как и любые незнакомые поля
        (Crawl-delay, Host, Sitemap, Clean-param).
        """
        groups: dict[str, list[_Rule]] = {}
        current: list[str] = []
        collecting_agents = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            raw_field, _, raw_value = line.partition(":")
            field = raw_field.strip().lower()
            value = raw_value.strip()
            if field == "user-agent":
                if not collecting_agents:
                    current = []
                    collecting_agents = True
                agent = value.lower()
                current.append(agent)
                groups.setdefault(agent, [])
                continue
            # Любая не-User-agent строка закрывает набор агентов группы: следующая
            # строка User-agent начнёт уже новую группу (грамматика RFC 9309 §2.2).
            collecting_agents = False
            if field not in ("allow", "disallow") or not current or not value:
                continue
            rule = _Rule(allow=field == "allow", pattern=value, matcher=_compile_pattern(value))
            for agent in current:
                groups[agent].append(rule)
        return cls(groups)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        """Побеждает правило с самым длинным совпавшим шаблоном.

        При равной длине побеждает Allow; если не совпало ни одно правило —
        доступ разрешён (RFC 9309 §2.2.2).
        """
        winner = self.matched_rule(user_agent, url)
        return winner is None or winner.allow

    def matched_rule(self, user_agent: str, url: str) -> _Rule | None:
        """Правило, определившее вердикт. Нужно для диагностики и тестов."""
        rules = self._rules_for(user_agent)
        target = _target_path(url)
        best: _Rule | None = None
        for rule in rules:
            if rule.matcher.match(target) is None:
                continue
            if best is None or len(rule.pattern) > len(best.pattern):
                best = rule
            elif len(rule.pattern) == len(best.pattern) and rule.allow:
                best = rule
        return best

    def _rules_for(self, user_agent: str) -> list[_Rule]:
        """Выбирает группу по токену продукта; иначе группу `*`; иначе пусто."""
        tokens = _product_tokens(user_agent)
        best_name: str | None = None
        for name in self._groups:
            if name == "*" or name not in tokens:
                continue
            if best_name is None or len(name) > len(best_name):
                best_name = name
        if best_name is not None:
            return self._groups[best_name]
        return self._groups.get("*", [])


def _looks_like_robots(response: httpx.Response) -> bool:
    """Отличает настоящий robots.txt от HTML-заглушки, отданной со статусом 200.

    Антибот-страница разбирается как «правил нет» и даёт fail-open, поэтому такой
    ответ трактуется как недоступность источника.
    """
    media_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if media_type and media_type != "text/plain":
        return False
    return not response.text.lstrip().startswith("<")


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
        # follow_redirects=False намеренно: httpx уводил бы на чужой хост молча,
        # мимо robots.txt и мимо паузы между запросами. Редиректы обходим сами.
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=config.timeout_sec,
            follow_redirects=False,
            transport=transport,
        )
        self._last_request_at: float | None = None
        self._robots: dict[str, Robots] = {}

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
        """Выполняет GET, проверяя robots.txt и выдерживая паузу на каждом хопе.

        Редирект уводит на другой хост (hh.ru -> hh.kz, rabota.by), у которого
        свой robots.txt, поэтому проверка обязана повторяться после каждого
        перенаправления, а не один раз для исходного URL.
        """
        if self._client.is_closed:
            # httpx бросил бы RuntimeError мимо иерархии ошибок приложения, и
            # прогон упал бы целиком вместо штатного частичного отказа.
            raise FetchFailed(f"HTTP-клиент уже закрыт, запрос {url} не выполнен")
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response = self._get_once(current, conditional)
            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = str(httpx.URL(current).join(location))
                continue
            return response
        raise FetchFailed(f"слишком много редиректов при получении {url}")

    def _get_once(self, url: str, conditional: dict[str, str] | None) -> httpx.Response:
        # Нормализация в самом начале, ДО проверки robots: дальше по этому
        # методу и матчер, и `self._client.get` видят одну и ту же строку,
        # поэтому щель между «что проверили» и «что ушло» закрыта по
        # построению, а не дисциплиной каждой новой точки входа в URL.
        url = normalize(url)
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
        """Разбирает Retry-After по RFC 9110: delay-seconds (в т.ч. дробные) или HTTP-date.

        "nan" отбрасывается отдельно: float("nan") не бросает ValueError, а любое
        сравнение с NaN ложно, из-за чего он проходит сквозь min() в _backoff()
        неизменным и улетает в sleep(). Бесконечность (inf), напротив, безопасна —
        min(inf, MAX_RETRY_AFTER_SEC) корректно даёт потолок, поэтому она не
        отбрасывается здесь.
        """
        text = value.strip()
        try:
            seconds = float(text)
        except ValueError:
            seconds = None
        if seconds is not None:
            if math.isnan(seconds):
                return None
            return max(seconds, 0.0)
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max((target - datetime.now(UTC)).total_seconds(), 0.0)

    def _check_robots(self, url: str) -> None:
        """Проверяет НОРМАЛИЗОВАННЫЙ URL по robots.txt его хоста.

        На вход обязан приходить результат `normalize()` — то есть ровно та
        строка, которую отправит httpx. Проверять что-либо иное значит
        выносить вердикт не о том запросе, который уйдёт в сеть.

        Кэш — по origin (scheme://netloc), а не один объект на клиента: иначе
        правила hh.ru молча применялись бы к hh.kz и rabota.by, куда уводят
        редиректы региональных вакансий.
        """
        if not self._config.respect_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots = self._robots.get(origin)
        if robots is None:
            robots = self._load_robots(origin)
            self._robots[origin] = robots
        if not robots.can_fetch(self._user_agent, url):
            winner = robots.matched_rule(self._user_agent, url)
            rule = f" (правило: Disallow: {winner.pattern})" if winner else ""
            raise RobotsDisallowed(f"robots.txt запрещает {url}{rule}")

    def _load_robots(self, origin: str) -> Robots:
        """Загружает robots.txt тем же путём, что и обычные запросы (с троттлингом).

        Недоступность (5xx, сетевая ошибка, непонятное тело) трактуется как полный
        запрет по умолчанию (RFC 9309 §2.3.1), а 404 — как отсутствие ограничений.
        """
        robots_url = f"{origin}/robots.txt"
        response = self._fetch_robots(robots_url)
        if response.status_code == 404:
            return Robots.parse("")
        if response.status_code != 200:
            logger.warning(
                "robots.txt ответил %s, доступ запрещён по умолчанию", response.status_code
            )
            # 403 именно здесь — почти наверняка не правило, а блокировка:
            # robots.txt отдают анонимно все. Отказ остаётся RobotsDisallowed
            # (мы действительно не знаем правил и поэтому не идём никуда), но
            # читающий лог обязан понимать, что чинить надо не конфиг.
            blocked = (
                " Это похоже на блокировку источника, а не на правило: robots.txt "
                "отдаётся анонимно. Обходные пути не применяются."
                if response.status_code == 403
                else ""
            )
            raise RobotsDisallowed(
                f"robots.txt вернул {response.status_code} для {robots_url}, "
                f"доступ запрещён по умолчанию.{blocked}"
            )
        if not _looks_like_robots(response):
            logger.warning(
                "robots.txt для %s не похож на robots.txt (Content-Type: %s), "
                "доступ запрещён по умолчанию",
                origin,
                response.headers.get("Content-Type", "<нет>"),
            )
            raise RobotsDisallowed(
                f"{robots_url} вернул 200, но тело не похоже на robots.txt "
                "(вероятно, заглушка антибота); доступ запрещён по умолчанию"
            )
        return Robots.parse(response.text)

    def _fetch_robots(self, robots_url: str) -> httpx.Response:
        """Качает сам robots.txt: без проверки robots (RFC 9309 её не требует),
        но с паузой на каждом хопе и с ограничением числа редиректов."""
        current = robots_url
        for _ in range(MAX_REDIRECTS + 1):
            self._throttle()
            try:
                response = self._client.get(current)
            except httpx.HTTPError as error:
                logger.warning("robots.txt недоступен (%s), доступ запрещён по умолчанию", error)
                raise RobotsDisallowed(
                    f"robots.txt недоступен для {robots_url}, доступ запрещён по умолчанию"
                ) from error
            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = str(httpx.URL(current).join(location))
                continue
            return response
        raise RobotsDisallowed(
            f"слишком много редиректов при получении {robots_url}, доступ запрещён по умолчанию"
        )
