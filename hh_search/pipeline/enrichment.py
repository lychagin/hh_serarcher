"""Шаги 4–6: страница вакансии, оценка и локальный пересчёт (спека §4.1).

Единственный шаг конвейера, ходящий в сеть за вакансией, и потому
единственный, где ошибка стоит запроса к hh.ru. Отсюда два разделения,
без которых шаг теряет данные.

1. **Транспортный отказ ≠ отказ страницы.** `FetchFailed` от 503 или
   таймаута и `RobotsDisallowed` от временно недоступного robots.txt — это
   состояния СЕРВЕРА, а не вакансии. Жечь ими `enrich_attempts` значит
   терять всю очередь за одну аварию источника: при `interval_hours = 4` и
   `max_attempts = 3` двенадцати часов недоступности достаточно, чтобы вся
   очередь ушла в `rejected`/`enrich_failed` терминально, откуда её не
   возвращает ничто (`add_discovered` даёт False, `pending_enrichment`
   требует `description IS NULL`). Спека §9 для этой строки требует лишь
   `WARNING` и `partial`. Счётчик уместен там, где отказ про саму вакансию:
   404, отсутствие `JobPosting`, пустое `description`.
   Плата за это решение названа честно: при длительной аварии вакансия
   перепрашивается каждый прогон. Дешевле её сделать `next_attempt_at` с
   экспоненциальным backoff, но это колонка в схеме, то есть правка
   задачи 6, и заказывать её надо явно. Терять данные ради экономии
   запросов — не тот размен, который выбирала спека.
2. **Отказ оценки ≠ отказ страницы.** Скоринг — чисто локальное
   вычисление, а страница за спиной уже стоила запроса. Поэтому
   исключение из `scorer.score` сохраняет страницу через
   `save_description` и оставляет вакансию в `pending_scoring`: в сеть за
   ней больше не пойдёт никто (спека §5.2).
"""

import logging

from hh_search.config.models import Config
from hh_search.domain.models import DiscoveredVacancy, VacancyDetails
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.pipeline.forbidden import ForbiddenStreak
from hh_search.pipeline.stats import PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sources.http import PoliteClient
from hh_search.sources.vacancy_page import SalaryBlockStats, parse_vacancy_page, vacancy_url
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)


def enrich(
    config: Config,
    client: PoliteClient,
    repo: SqliteRepository,
    scorer: Scorer,
    stats: RunStats,
    forbidden: ForbiddenStreak,
) -> None:
    """Скачать страницы очереди обогащения, оценить и сохранить."""
    pending = repo.pending_enrichment(config.app.enrich.max_attempts)
    salary_stats = SalaryBlockStats()
    unavailable = 0
    unreadable = 0
    for vacancy in pending:
        # URL собирается заново, а не берётся из базы: канонический
        # `https://hh.ru/vacancy/{id}` без query-строки — единственная
        # форма, разрешённая живым robots.txt (см. sources/listing.py).
        url = vacancy_url(vacancy.id)
        try:
            response = client.get(url)
        except AccessForbidden as error:
            # Тоже источник, а не вакансия: попытку НЕ жжём. Одиночный 403
            # бывает антиботом на конкретном запросе, и терять из-за него
            # уже скачанные семнадцать страниц из двадцати незачем;
            # устойчивый `tolerate` пробросит сам (спека §9).
            unavailable += 1
            forbidden.tolerate(error, f"страница {url}", stats)
            continue
        except (FetchFailed, RobotsDisallowed) as error:
            # Источник, а не вакансия: попытку НЕ жжём.
            unavailable += 1
            stats.degrade(PARTIAL, f"страница {url} не получена: {error}")
            logger.warning("страница %s недоступна, попытка не израсходована: %s", url, error)
            continue
        forbidden.survived()
        if response.status_code != 200:
            _burn_attempt(config, repo, stats, vacancy.id, f"код {response.status_code}")
            unreadable += 1
            continue
        try:
            details = parse_vacancy_page(response.text, salary_stats)
        except FetchFailed as error:
            _burn_attempt(config, repo, stats, vacancy.id, str(error))
            unreadable += 1
            continue
        if _save(repo, scorer, vacancy, details, stats):
            # Накапливаем по ходу, а не присваиваем в конце: падение на
            # шестнадцатой из двадцати обязано оставить в журнале
            # пятнадцать, а не ноль.
            stats.enriched += 1
    salary_stats.log_summary()
    _canary(len(pending), unavailable, unreadable)
    _check_not_stalled(config, repo, stats)


def _burn_attempt(
    config: Config, repo: SqliteRepository, stats: RunStats, vacancy_id: str, reason: str
) -> None:
    """Отказ про саму вакансию: инкремент попытки и, при исчерпании, отказ.

    Терминальный статус ставит тем же UPDATE сам `bump_enrich_attempt` —
    отдельного `mark_rejected` здесь нет сознательно: пара вызовов
    оставляла между собой состояние, невидимое всем трём выборкам
    (спека §5.2).
    """
    attempts = repo.bump_enrich_attempt(vacancy_id, config.app.enrich.max_attempts)
    stats.degrade(PARTIAL, f"вакансия {vacancy_id} не обогащена: {reason}")
    if attempts >= config.app.enrich.max_attempts:
        logger.warning(
            "вакансия %s: попытка %d из %d, лимит исчерпан, отказ enrich_failed: %s",
            vacancy_id,
            attempts,
            config.app.enrich.max_attempts,
            reason,
        )
    else:
        logger.warning(
            "вакансия %s: попытка %d из %d не удалась: %s",
            vacancy_id,
            attempts,
            config.app.enrich.max_attempts,
            reason,
        )


def _save(
    repo: SqliteRepository,
    scorer: Scorer,
    vacancy: DiscoveredVacancy,
    details: VacancyDetails,
    stats: RunStats,
) -> bool:
    """Оценить и записать. Отказ оценки не выбрасывает скачанную страницу."""
    try:
        score = scorer.score(vacancy, details)
    except Exception as error:  # noqa: BLE001 — страница дороже оценки
        repo.save_description(vacancy.id, details)
        stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не посчиталась: {error}")
        logger.error(
            "оценка вакансии %s не посчиталась (%s); страница сохранена без оценки и "
            "будет досчитана локально, в сеть за ней больше не идём",
            vacancy.id,
            error,
            exc_info=True,
        )
        return False
    try:
        repo.save_enriched(vacancy.id, details, score)
    except ValueError as error:
        # Оценка не сериализуется. Описание `save_enriched` сохранил сам,
        # поэтому здесь остаётся только не уронить прогон.
        stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не сериализуется: {error}")
        logger.error("оценка вакансии %s не сериализуется: %s", vacancy.id, error, exc_info=True)
        return False
    return True


def _check_not_stalled(config: Config, repo: SqliteRepository, stats: RunStats) -> None:
    """Сторож правки `enrich.max_attempts` вниз — счёт после шага, а не до.

    Считать надо именно здесь: строки, честно исчерпавшие лимит В ЭТОМ
    прогоне, `bump_enrich_attempt` уже сделал терминальными
    (`rejected`/`enrich_failed`) тем же оператором, и в счёт они не
    попадают. Остаться `status='new'` с пустым описанием и счётчиком не
    меньше лимита строка может ровно по одной причине — лимит понизили
    после того, как попытки были потрачены.

    Понижение статуса симметрично `stuck`: вакансия, уже стоившая
    запросов к hh.ru, не попадёт ни в один отчёт и не видна ни одной
    выборке. Молчать об этом нельзя — прогон после такой правки
    становился `ok`, то есть статус улучшался оттого, что работа пропала.
    """
    stalled = repo.stalled_by_attempts(config.app.enrich.max_attempts)
    stats.stalled = stalled
    if not stalled:
        return
    stats.degrade(PARTIAL, f"{stalled} вакансий выведены из очереди снижением лимита попыток")
    logger.error(
        "%d вакансий без описания имеют enrich_attempts >= %d и потому не видны НИ ОДНОЙ "
        "из трёх выборок: лимит попыток (enrich.max_attempts) понижен уже после того, как "
        "попытки были потрачены. Скачивания не было — вернуть их можно, подняв лимит "
        "обратно либо командой `mark <id> new` (она обнуляет счётчик). Найти: "
        "SELECT id FROM vacancy WHERE status='new' AND description IS NULL "
        "AND enrich_attempts >= %d",
        stalled,
        config.app.enrich.max_attempts,
        config.app.enrich.max_attempts,
    )


def _canary(pending: int, unavailable: int, unreadable: int) -> None:
    """Тревога на смену вёрстки и на аварию источника — спека §9.

    Порог «больше половины» ловит проблему в тот же день, а не через месяц
    по пустым отчётам. Две причины разведены, потому что лечатся они
    по-разному: вёрстку правит разработчик, аварию — время.
    """
    if not pending:
        return
    if unreadable * 2 > pending:
        logger.error(
            "не разобрано %d страниц вакансий из %d — вероятно, hh.ru сменил вёрстку "
            "страницы вакансии или разметку JSON-LD",
            unreadable,
            pending,
        )
    if unavailable * 2 > pending:
        logger.error(
            "не получено %d страниц вакансий из %d — похоже, источник недоступен; "
            "попытки не израсходованы, очередь сохранена до следующего прогона",
            unavailable,
            pending,
        )


def rescore(repo: SqliteRepository, scorer: Scorer, stats: RunStats) -> int:
    """Шаг 6: локальный пересчёт оценок. Сеть не задействуется.

    Обслуживает две очереди сразу: вакансии, у которых оценка не
    посчиталась при обогащении (`save_description` выше), и те, у которых
    оценку обнулил карантин, прочитав её как испорченную.
    """
    rescored = 0
    for vacancy, details in repo.pending_scoring():
        try:
            repo.save_score(vacancy.id, scorer.score(vacancy, details))
        except Exception as error:  # noqa: BLE001 — одна вакансия не роняет прогон
            stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не пересчиталась: {error}")
            logger.error(
                "оценка вакансии %s не пересчиталась: %s", vacancy.id, error, exc_info=True
            )
            continue
        rescored += 1
    return rescored
