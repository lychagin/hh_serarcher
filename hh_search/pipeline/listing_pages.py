"""Одна страница листинга целиком: забрать, разобрать, записать (спека
2026-08-01 §1).

Отделено от `discovery.py` по единице работы, а не по слою: там цикл по
листингам и страницам плюс агрегатный сторож «прогон не может быть
пустым», здесь — всё, что происходит с ОДНОЙ страницей, включая повтор
вырожденного ответа. Держать их вместе значило бы файл за границей
бюджета в 150 строк.

Порядок записи прежний и не стилистический: валидатор условного запроса
сохраняется ПОСЛЕ того, как все вакансии страницы оказались в базе.
Обратный порядок оставляет в `http_cache` валидатор снимка, который
никогда не был прочитан.
"""

import logging
from dataclasses import dataclass

import httpx

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy
from hh_search.errors import AccessForbidden, DegenerateListing, FetchFailed, RobotsDisallowed
from hh_search.pipeline.stats import PARTIAL, RunStats
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import parse_listing
from hh_search.storage.base import Repository

logger = logging.getLogger(__name__)

OK_STATUS = 200


@dataclass
class DegenerateDigest:
    """Счётчик вырожденных страниц: одна ничего не значит, все — дрейф.

    По одной странице сказать нельзя ничего: вырожденный ответ измерен
    как рядовая помеха частотой ~1 из 9. Но прогон, где повтор
    понадобился КАЖДОЙ отданной странице, означает не помеху, а то, что
    hh.ru перестал отдавать `ItemList` с первого раза, — и такое обязано
    быть слышно. Тот же приём, что у `WorkFormatBlockStats`.
    """

    pages: int = 0
    degenerate: int = 0
    rescued: int = 0

    def log_summary(self) -> None:
        if not self.degenerate:
            return
        if self.degenerate == self.pages:
            logger.warning(
                "все %d отданных страниц листингов пришли без блока ItemList, повтор спас "
                "%d: это уже не рядовая помеха источника, а похоже на смену разметки",
                self.pages,
                self.rescued,
            )
        else:
            logger.info(
                "страниц листингов без блока ItemList: %d из %d, повтор спас %d",
                self.degenerate,
                self.pages,
                self.rescued,
            )


def store_page(
    repo: Repository,
    query: QuerySpec,
    url: str,
    response: httpx.Response,
    stats: RunStats,
    seen: set[str],
    client: PoliteClient,
    digest: DegenerateDigest,
) -> None:
    """Разобрать страницу, записать вакансии и только потом — валидатор."""
    digest.pages += 1
    try:
        vacancies, final = _parse_with_retry(url, response, query.slug, client, digest)
    except FetchFailed as error:
        # Валидатор не сохраняем и вычищаем прежний: 304 на следующем
        # прогоне спрятал бы дрейф формата за нулевой работой, а один
        # лишний полный ответ — дешевле месяца молчания.
        repo.reset_cache(url)
        stats.degrade(PARTIAL, f"листинг {url} не разобран: {error}")
        logger.error("листинг %s не разобран, кэш условного запроса сброшен: %s", url, error)
        return
    for vacancy in vacancies:
        # Запись идёт в любом случае, а счёт — только на первой встрече.
        # Пропускать повтор целиком нельзя: `add_discovered` идемпотентен и
        # именно он решает судьбу кластера (охрана `cluster_weight <`), а
        # `new_count` уже верен — на известный id метод отвечает False.
        if repo.add_discovered(vacancy, query.cluster, query.weight):
            stats.new_count += 1
        if vacancy.id not in seen:
            seen.add(vacancy.id)
            stats.discovered += 1
    # Валидаторы берутся от ТОГО ответа, который разобрался: сохрани мы
    # заголовки вырожденной страницы, следующий прогон получил бы на неё
    # 304 и не увидел бы вакансий вовсе.
    repo.save_cache_headers(url, final.headers.get("ETag"), final.headers.get("Last-Modified"))


def _parse_with_retry(
    url: str,
    response: httpx.Response,
    slug: str,
    client: PoliteClient,
    digest: DegenerateDigest,
) -> tuple[list[DiscoveredVacancy], httpx.Response]:
    """Разбор с одним безусловным повтором на вырожденный ответ.

    Повторяется РОВНО `DegenerateListing` и ничего больше: остальные
    четыре отказа `parse_listing` постоянны (несуществующий slug вернёт то
    же самое) либо означают дрейф разметки, где правильное поведение —
    остановиться и закричать, а не удваивать запросы.

    Повтор безусловный: первый запрос уже получил 200, и `If-None-Match`
    вернул бы `304` с пустым телом. Пауза берётся сама — троттлинг живёт
    в клиенте.
    """
    try:
        return parse_listing(response.text, slug), response
    except DegenerateListing as error:
        digest.degenerate += 1
        logger.warning("листинг %s пришёл без блока ItemList, повторяем запрос: %s", url, error)
    try:
        retry = client.get(url)
    except (AccessForbidden, FetchFailed, RobotsDisallowed) as error:
        # Отдельной ветки у «повтор упал сам» нет намеренно: с точки
        # зрения прогона это тот же потерянный листинг, и различать их
        # значило бы плодить состояния без разных последствий. 403 при
        # этом НЕ пробрасывается наружу: устойчивую серию считает
        # `ForbiddenStreak` по первым запросам, а повтор — наша
        # инициатива, и обрывать ею прогон нечестно.
        raise FetchFailed(f"повторный запрос не удался: {error}") from error
    if retry.status_code != OK_STATUS:
        raise FetchFailed(f"повторный запрос вернул код {retry.status_code}")
    vacancies = parse_listing(retry.text, slug)
    digest.rescued += 1
    return vacancies, retry
