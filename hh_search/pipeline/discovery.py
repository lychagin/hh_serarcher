"""Шаги 1–3: листинги, дедупликация, префильтр (спека §4.1).

Порядок записи внутри шага 1 — не стилистический. Валидатор условного
запроса (`ETag`/`Last-Modified`) сохраняется ПОСЛЕ того, как все вакансии
страницы оказались в базе, и никогда раньше. Обратный порядок означал, что
любой отказ между этими точками оставляет в `http_cache` валидатор
снимка, который никогда не был прочитан: дальше `If-None-Match` даёт 304,
страница не разбирается вообще, и прогон честно сообщает `ok` при нулевой
работе. Для воспроизведения не нужна авария — достаточно, чтобы hh.ru
один раз отдал обрезанную выдачу. Это класс отказа «месяцы молчания при
зелёном healthcheck», и стоит он всех вакансий сразу.
"""

import logging

import httpx

from hh_search.config.models import Config, QuerySpec
from hh_search.errors import FetchFailed, RobotsDisallowed
from hh_search.filtering.prefilter import Prefilter
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import build_listing_url, parse_listing
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)

NOT_MODIFIED = 304


def discover(config: Config, client: PoliteClient, repo: SqliteRepository, stats: RunStats) -> None:
    """Обойти все листинги и все их страницы; каждая страница — один запрос."""
    fetched = 0
    unchanged = 0
    for query in config.queries.queries:
        for page in range(query.pages):
            url = build_listing_url(query, page)
            try:
                response = client.get(url, conditional=repo.cache_headers(url))
            except (FetchFailed, RobotsDisallowed) as error:
                # Состояние СЕРВЕРА, а не листинга: следующий прогон
                # повторит запрос, терять нечего. Спека §9 — WARNING+partial.
                stats.degrade(PARTIAL, f"листинг {url} не получен: {error}")
                logger.warning("листинг %s пропущен: %s", url, error)
                continue
            if response.status_code == NOT_MODIFIED:
                unchanged += 1
                logger.debug("листинг %s не изменился", url)
                continue
            if response.status_code != 200:
                stats.degrade(PARTIAL, f"листинг {url}: код {response.status_code}")
                logger.warning("листинг %s ответил %s", url, response.status_code)
                continue
            # Считается ОТДАННАЯ источником страница, а не успешно
            # разобранная: дрейф формата, при котором не разбирается ни
            # одна, — это и есть тишина, которую обязан поймать сторож
            # ниже. Считай мы разобранные, отказ разбора остался бы
            # `partial`, то есть успехом для healthcheck.
            fetched += 1
            _store_page(repo, query, url, response, stats)
    _check_not_silent(config, stats, fetched, unchanged)


def _store_page(
    repo: SqliteRepository,
    query: QuerySpec,
    url: str,
    response: httpx.Response,
    stats: RunStats,
) -> None:
    """Разобрать страницу, записать вакансии и только потом — валидатор."""
    try:
        vacancies = parse_listing(response.text, query.slug)
    except FetchFailed as error:
        # Валидатор не сохраняем и вычищаем прежний: 304 на следующем
        # прогоне спрятал бы дрейф формата за нулевой работой, а один
        # лишний полный ответ — дешевле месяца молчания.
        repo.reset_cache(url)
        stats.degrade(PARTIAL, f"листинг {url} не разобран: {error}")
        logger.error("листинг %s не разобран, кэш условного запроса сброшен: %s", url, error)
        return
    for vacancy in vacancies:
        stats.discovered += 1
        if repo.add_discovered(vacancy, query.cluster, query.weight):
            stats.new_count += 1
    repo.save_cache_headers(
        url, response.headers.get("ETag"), response.headers.get("Last-Modified")
    )


def _check_not_silent(config: Config, stats: RunStats, fetched: int, unchanged: int) -> None:
    """Агрегатный сторож: пустая страница законна, пустой ПРОГОН — нет.

    Пустой `itemListElement` — законный результат для ОДНОЙ страницы
    (короткий листинг, конец пагинации), поэтому `parse_listing` на нём
    молчит. Для всего прогона при непустом списке листингов он означает,
    что источник перестал отдавать выдачу, — и это ровно тот класс
    отказа, который стоил проекту раунда: месяцы тишины при зелёном
    healthcheck.

    `failed`, а не `partial`, потому что `partial` считается успехом для
    `last_successful_run()`, то есть для healthcheck. Сторож накрывает и
    дрейф формата: там отказывает разбор каждой страницы, и без этой
    строки прогон остался бы `partial`, то есть успешным.
    """
    if fetched and not stats.discovered:
        stats.degrade(
            FAILED, f"источник отдал {fetched} страниц листингов, вакансий не найдено ни одной"
        )
        logger.error(
            "источник отдал %d страниц листингов по %d запросам, и ни одна не дала ни "
            "одной вакансии. Либо блок ItemList пуст, либо разбор отказал на каждой "
            "странице (причина выше). Прогон помечен %s",
            fetched,
            len(config.queries.queries),
            FAILED,
        )
    elif unchanged and not fetched:
        logger.warning(
            "ни одна из %d страниц листингов не изменилась с прошлого прогона (304); "
            "новых вакансий в этом прогоне не будет",
            unchanged,
        )


def prefilter(config: Config, repo: SqliteRepository, stats: RunStats) -> None:
    """Шаг 3: отсев по заголовку — единственный барьер перед сетью.

    Идёт по всей очереди обогащения, а не только по найденному сейчас:
    отсев локальный и бесплатный, а правка `negative` в конфиге обязана
    доставать накопленный бэклог, а не только следующую находку.
    """
    barrier = Prefilter(config.profile)
    for vacancy in repo.pending_enrichment(config.app.enrich.max_attempts):
        reason = barrier.reason_to_reject(vacancy)
        if reason is not None:
            repo.mark_rejected(vacancy.id, reason)
            stats.rejected += 1
