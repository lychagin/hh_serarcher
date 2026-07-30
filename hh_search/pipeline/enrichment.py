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
from hh_search.pipeline.failures import FailureDigest
from hh_search.pipeline.forbidden import ForbiddenStreak
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sources.http import PoliteClient
from hh_search.sources.vacancy_page import SalaryBlockStats, parse_vacancy_page, vacancy_url
from hh_search.sources.work_format import WorkFormatBlockStats
from hh_search.storage.base import Repository

logger = logging.getLogger(__name__)


def enrich(
    config: Config,
    client: PoliteClient,
    repo: Repository,
    scorer: Scorer,
    stats: RunStats,
    forbidden: ForbiddenStreak,
) -> None:
    """Скачать страницы очереди обогащения, оценить и сохранить."""
    pending = repo.pending_enrichment(
        config.app.enrich.max_attempts, config.app.limits.enrich_per_run
    )
    salary_stats = SalaryBlockStats()
    # Симметрично salary_stats: второе отступление от «структурированные
    # данные, а не вёрстка» разрешено владельцем под тем же условием — дрейф
    # обязан быть заметным. Сторож, который существует классом, но никогда
    # не создаётся в проде, — мёртвый код и невыполненное условие разом.
    work_format_stats = WorkFormatBlockStats()
    # Отказы копятся, а не печатаются по одному: при аварии источника они
    # отличаются только URL (см. pipeline/failures.py).
    skipped = FailureDigest()
    retried = FailureDigest()
    exhausted = FailureDigest()
    unavailable = 0
    # Страницы, ОТДАННЫЕ источником (200), и страницы, которые удалось
    # разобрать. Пара нужна сторожу ниже: только она отличает дрейф
    # вёрстки от недоступности источника, а «не разобрана ни одна» —
    # от «разобрано меньше половины».
    answered = 0
    parsed = 0
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
            skipped.add(str(error), url)
            continue
        forbidden.survived()
        if response.status_code != 200:
            _burn_attempt(
                config, repo, stats, vacancy.id, f"код {response.status_code}", retried, exhausted
            )
            continue
        answered += 1
        try:
            details = parse_vacancy_page(response.text, salary_stats, work_format_stats)
        except FetchFailed as error:
            _burn_attempt(config, repo, stats, vacancy.id, str(error), retried, exhausted)
            continue
        parsed += 1
        if _save(repo, scorer, vacancy, details, stats):
            # Накапливаем по ходу, а не присваиваем в конце: падение на
            # шестнадцатой из двадцати обязано оставить в журнале
            # пятнадцать, а не ноль.
            stats.enriched += 1
    salary_stats.log_summary()
    work_format_stats.log_summary()
    skipped.log_summary("страниц вакансий не получено, попытки не израсходованы")
    retried.log_summary(
        f"вакансий не обогащено, попытка израсходована (лимит {config.app.enrich.max_attempts})"
    )
    # Терминальные потери — со ВСЕМИ id: эти вакансии больше не увидит ни
    # одна выборка, и чинить их придётся поимённо.
    exhausted.log_summary("вакансий закрыты как enrich_failed, лимит попыток исчерпан", limit=None)
    _canary(stats, len(pending), answered, parsed, unavailable, retried.count + exhausted.count)
    _check_not_stalled(config, repo, stats)


def _burn_attempt(
    config: Config,
    repo: Repository,
    stats: RunStats,
    vacancy_id: str,
    reason: str,
    retried: FailureDigest,
    exhausted: FailureDigest,
) -> None:
    """Отказ про саму вакансию: инкремент попытки и, при исчерпании, отказ.

    Терминальный статус ставит тем же UPDATE сам `bump_enrich_attempt` —
    отдельного `mark_rejected` здесь нет сознательно: пара вызовов
    оставляла между собой состояние, невидимое всем трём выборкам
    (спека §5.2).

    Печатается отказ не здесь, а сводкой в конце шага: при дрейфе вёрстки
    причина у всех вакансий одна, и восемнадцать одинаковых строк её не
    уточняют. Терминальные и непотраченные разведены по разным копилкам,
    потому что означают разное: первое — потеря вакансии навсегда.
    """
    attempts = repo.bump_enrich_attempt(vacancy_id, config.app.enrich.max_attempts)
    stats.degrade(PARTIAL, f"вакансия {vacancy_id} не обогащена: {reason}")
    digest = exhausted if attempts >= config.app.enrich.max_attempts else retried
    digest.add(reason, vacancy_id)


def _save(
    repo: Repository,
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


def _check_not_stalled(config: Config, repo: Repository, stats: RunStats) -> None:
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
        "обратно либо командой `mark <id> new` (она обнуляет счётчик). Найти: %s",
        stalled,
        config.app.enrich.max_attempts,
        # Запрос спрашивается у хранилища, а не пишется здесь: SQL — знание
        # слоя storage (§4.3), и совет, написанный в конвейере, протухает
        # молча при первой же правке схемы.
        repo.stalled_rows_hint(config.app.enrich.max_attempts),
    )


def _canary(
    stats: RunStats, pending: int, answered: int, parsed: int, unavailable: int, unreadable: int
) -> None:
    """Тревога на смену вёрстки и на аварию источника — спека §9.

    Порог «больше половины» ловит проблему в тот же день, а не через месяц
    по пустым отчётам. Две причины разведены, потому что лечатся они
    по-разному: вёрстку правит разработчик, аварию — время.

    Крайний случай «источник страницы ОТДАЛ, разобрать не удалось ни одну»
    вынесен из порога отдельной ветвью и понижает статус до `failed` —
    симметрично `discovery._check_not_silent`, где та же тишина уровнем
    выше решена именно так. Причина та же: `partial` считается успехом для
    `last_successful_run()`, то есть для healthcheck, а прогон, не
    разобравший ни одной страницы, работы не сделал вовсе. Замер на
    FIX_BASE (живой листинг, страница вакансии без JSON-LD): три прогона
    `partial`, четвёртый — `ok` с нулём обогащённых, `healthcheck` зелен,
    отчётов нет, весь бэклог терминально в `enrich_failed`. Обе ветви
    плохи, и лечит их одна: пока вакансии в очереди есть, дрейф вёрстки
    красит индикатор в красный.

    `answered`, а не `pending`, в условии — это не деталь. Считать надо
    страницы, которые источник ОТДАЛ (код 200): недоступность источника и
    ответ 404 на снятой вакансии — не дрейф формата, и делать из них
    `failed` значило бы красить индикатор при живом сервисе. Ровно так же
    в `discovery` считаются отданные страницы листингов, а не запрошенные.

    Цена названа честно: если в очереди была ровно одна вакансия и её
    страница не разобралась, прогон тоже станет `failed`, хотя беда
    частная. Красным индикатор при этом побудет не дольше
    `enrich.max_attempts` прогонов — дальше строка терминальна и в
    очередь не попадает. Обратный размен (требовать «хотя бы N страниц»)
    означал бы, что дрейф вёрстки на маленькой очереди проходит молча, а
    молчание здесь и есть чинимая беда. Тот же выбор сделан уровнем выше:
    одна страница листинга с пустым `ItemList` тоже даёт `failed`.
    """
    if not pending:
        return
    if answered and not parsed:
        stats.degrade(FAILED, f"источник отдал {answered} страниц вакансий, не разобрана ни одна")
        logger.error(
            "источник отдал %d страниц вакансий, и не разобрана НИ ОДНА — вероятно, hh.ru "
            "сменил вёрстку страницы вакансии или разметку JSON-LD. Прогон помечен %s: "
            "иначе сервис молча перестал бы обогащать вакансии, оставаясь зелёным для "
            "healthcheck, пока очередь уходит в enrich_failed безвозвратно",
            answered,
            FAILED,
        )
    elif unreadable * 2 > pending:
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


def rescore(repo: Repository, scorer: Scorer, stats: RunStats, limit: int) -> int:
    """Шаг 6: локальный пересчёт оценок. Сеть не задействуется.

    Обслуживает две очереди сразу: вакансии, у которых оценка не
    посчиталась при обогащении (`save_description` выше), и те, у которых
    оценку обнулил карантин, прочитав её как испорченную.

    `limit` здесь не про сеть, а про память: строки очереди несут
    описание, то есть стоят столько же, сколько строки отчёта.
    """
    rescored = 0
    for vacancy, details in repo.pending_scoring(limit):
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
