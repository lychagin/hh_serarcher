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
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.filtering.prefilter import Prefilter
from hh_search.pipeline.failures import FailureDigest
from hh_search.pipeline.forbidden import ForbiddenStreak
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import build_listing_url, parse_listing
from hh_search.storage.base import REJECT_CODE_PREFILTER, Repository

logger = logging.getLogger(__name__)

NOT_MODIFIED = 304


def discover(
    config: Config,
    client: PoliteClient,
    repo: Repository,
    stats: RunStats,
    forbidden: ForbiddenStreak,
) -> None:
    """Обойти все листинги и все их страницы; каждая страница — один запрос."""
    fetched = 0
    unchanged = 0
    # Отказы копятся и печатаются сводкой: при недоступном источнике они
    # отличаются только URL, а причина у всех одна (см. pipeline/failures.py).
    skipped = FailureDigest()
    for query in config.queries.queries:
        # Дедупликация на весь ЛИСТИНГ, а не на страницу. Пагинация hh.ru
        # сдвигается между запросами (выдача живая, вакансии добавляются и
        # снимаются), поэтому повтор на границе страниц — обычное дело, а
        # не авария. `parse_listing` умеет отсеивать повтор только внутри
        # одной страницы, и `stats.discovered` из-за этого считал одну
        # вакансию дважды; `new_count` при этом всегда был верен —
        # `add_discovered` отвечает False на уже известный id.
        # Между РАЗНЫМИ листингами дедупликации нет сознательно: одна и та
        # же вакансия, найденная двумя запросами, — это две находки, и
        # именно они определяют кластер (побеждает больший weight).
        seen: set[str] = set()
        for page in range(query.pages):
            url = build_listing_url(query, page)
            try:
                response = client.get(url, conditional=repo.cache_headers(url))
            except AccessForbidden as error:
                # Одиночный 403 бывает антиботом на конкретном запросе;
                # устойчивый метод пробросит сам (спека §9).
                forbidden.tolerate(error, f"листинг {url}", stats)
                continue
            except (FetchFailed, RobotsDisallowed) as error:
                # Состояние СЕРВЕРА, а не листинга: следующий прогон
                # повторит запрос, терять нечего. Спека §9 — WARNING+partial.
                stats.degrade(PARTIAL, f"листинг {url} не получен: {error}")
                skipped.add(str(error), url)
                continue
            forbidden.survived()
            if response.status_code == NOT_MODIFIED:
                unchanged += 1
                logger.debug("листинг %s не изменился", url)
                continue
            if response.status_code != 200:
                stats.degrade(PARTIAL, f"листинг {url}: код {response.status_code}")
                skipped.add(f"код ответа {response.status_code}", url)
                continue
            # Считается ОТДАННАЯ источником страница, а не успешно
            # разобранная: дрейф формата, при котором не разбирается ни
            # одна, — это и есть тишина, которую обязан поймать сторож
            # ниже. Считай мы разобранные, отказ разбора остался бы
            # `partial`, то есть успехом для healthcheck.
            fetched += 1
            _store_page(repo, query, url, response, stats, seen)
    skipped.log_summary("страниц листингов не получено")
    _check_not_silent(config, stats, fetched, unchanged)


def _store_page(
    repo: Repository,
    query: QuerySpec,
    url: str,
    response: httpx.Response,
    stats: RunStats,
    seen: set[str],
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
        # Запись идёт в любом случае, а счёт — только на первой встрече.
        # Пропускать повтор целиком нельзя: `add_discovered` идемпотентен и
        # именно он решает судьбу кластера (охрана `cluster_weight <`), а
        # `new_count` уже верен — на известный id метод отвечает False.
        # Врал только `discovered`, и правится ровно он.
        if repo.add_discovered(vacancy, query.cluster, query.weight):
            stats.new_count += 1
        if vacancy.id not in seen:
            seen.add(vacancy.id)
            stats.discovered += 1
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

    Двух ветвей здесь тоже мало было бы одной. Сторож висел только на
    страницах, ОТДАННЫХ источником (`fetched > 0`), и потому молчал ровно
    в самом громком случае — когда не получено НИ ОДНОЙ страницы. Полная
    потеря сети (или устойчивая недоступность robots.txt) роняет каждый
    запрос в `partial`, `partial` считается успехом, и `healthcheck`
    возвращает 0 вечно: «процесс жив, работа не делается» — тот самый
    класс, ради которого healthcheck и заводился. Воспроизведено
    2026-07-28 в офлайн-контейнере: `run.status='partial'`,
    `healthcheck` — код 0 с «ok, последний успешный прогон».
    Ноль отданных страниц при ненулевом числе запрошенных — это не
    частичная потеря, а отсутствие работы целиком. Ответ `304` при этом
    остаётся успехом: источник ответил, и ответил «не изменилось».

    Из второй ветви по той же причине вычтен случай «есть хотя бы один
    `304`». Он и есть улика исправности: источник страницу узнал и
    сказал, что она не менялась, — значит единственная СВЕЖАЯ страница без
    вакансий это пустой хвост пагинации (`pages: 2`, на втором листе
    вакансии кончились), а не отказ. Прежний код красил такой прогон в
    `failed`, то есть красил healthcheck при исправном сервисе.
    Достижимо это только если hh.ru отдаёт валидаторы условного запроса на
    HTML листинга — на живом источнике не подтверждено, поэтому цена
    ошибки в обе стороны мала, а `failed` дороже.
    """
    requested = sum(query.pages for query in config.queries.queries)
    if requested and not fetched and not unchanged:
        stats.degrade(
            FAILED,
            f"источник не отдал ни одной из {requested} запрошенных страниц листингов",
        )
        logger.error(
            "ни одна из %d запрошенных страниц листингов не получена: источник недоступен "
            "целиком (сеть, robots.txt или блокировка — причина выше). Прогон помечен %s, "
            "иначе полная потеря сети выглядела бы для healthcheck успехом",
            requested,
            FAILED,
        )
    elif fetched and not stats.discovered and not unchanged:
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
    elif fetched and not stats.discovered:
        # Та же тишина, но с уликой обратного: хотя бы одна страница
        # ответила `304`, то есть источник жив, разбирался и не изменился.
        # Единственная СВЕЖАЯ страница при этом — пустой хвост пагинации
        # (`pages: 2`, на втором листе кончились вакансии), и объявлять
        # прогон `failed` из-за него значит красить индикатор в красный
        # на исправном сервисе. Дрейф формата этой веткой не прячется:
        # он отказывает разбором, а `_store_page` при отказе разбора
        # сбрасывает валидатор, поэтому уже следующий прогон получит на
        # эту страницу полный ответ, `unchanged` станет нулём — и сторож
        # выше сработает.
        logger.warning(
            "источник отдал %d свежих страниц листингов без единой вакансии, ещё %d "
            "не изменились (304): похоже на пустой хвост пагинации, а не на отказ. "
            "Статус прогона не понижен",
            fetched,
            unchanged,
        )
    elif unchanged and not fetched:
        logger.warning(
            "ни одна из %d страниц листингов не изменилась с прошлого прогона (304); "
            "новых вакансий в этом прогоне не будет",
            unchanged,
        )


def prefilter(config: Config, repo: Repository, stats: RunStats) -> None:
    """Шаг 3: отсев по заголовку — единственный барьер перед сетью.

    Идёт по всей очереди обогащения, а не только по найденному сейчас:
    отсев локальный и бесплатный, а правка `negative` в конфиге обязана
    доставать накопленный бэклог, а не только следующую находку.

    По той же причине шаг работает в ОБЕ стороны. Раньше он умел только
    отказывать, и отказ был вечным: слово, попавшее в `negative` по
    ошибке, убивало целевые вакансии навсегда — притом что решение о них
    чисто локальное, заголовок лежит в базе, и сеть для пересмотра не
    нужна вовсе. Возврат идёт ПЕРЕД отсевом, чтобы правка конфига
    отрабатывала за один прогон: возвращённая вакансия сразу попадает в
    `pending_enrichment` этого же прогона и доходит до отчёта, а не ждёт
    следующего.
    """
    barrier = Prefilter(config.profile)
    stats.requeued = _take_back(barrier, repo)
    # `pending_titles`, а не `pending_enrichment`: решение принимается по
    # одному заголовку, и лимита у него нет — отсев обязан накрывать
    # очередь ЦЕЛИКОМ. Читай он ограниченную выборку, вакансия, вытесненная
    # за границу окна отсева, но попавшая в окно обогащения после чужих
    # отказов, ушла бы в сеть, ни разу не пройдя единственный барьер перед
    # ней. Заодно шаг перестал строить `DiscoveredVacancy` из одиннадцати
    # колонок ради одного поля.
    for vacancy_id, title in repo.pending_titles(config.app.enrich.max_attempts):
        reason = barrier.reason_for_title(title)
        if reason is not None:
            repo.mark_rejected(vacancy_id, reason, REJECT_CODE_PREFILTER)
            stats.rejected += 1


def _take_back(barrier: Prefilter, repo: Repository) -> int:
    """Вернуть в очередь отказы префильтра, которые текущий конфиг не подтвердил.

    Ни одного обращения к сети: решение принимается по заголовку, а он
    лежит в базе с шага discovery. Ни одной записи, когда конфиг не
    менялся: список возвращаемых пуст, и `requeue_prefiltered` выходит
    до `UPDATE`. Выбираются только отказы с кодом `prefilter` —
    `enrich_failed` значит «страница не разбирается», и заголовок про это
    ничего сказать не может.
    """
    returning = [
        vacancy_id
        for vacancy_id, title in repo.rejected_by_prefilter()
        if barrier.reason_for_title(title) is None
    ]
    requeued = repo.requeue_prefiltered(returning)
    if requeued:
        logger.info(
            "стоп-слова больше не совпадают с %d ранее отбракованными вакансиями: %s. "
            "Они возвращены в очередь обогащения — переоценка локальная, в сеть за ней "
            "не ходили",
            requeued,
            ", ".join(returning),
        )
    return requeued
