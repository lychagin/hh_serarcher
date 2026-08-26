"""Оркестрация семи шагов конвейера (спека §4.1).

Хранилище здесь и во всех модулях шага — это протокол
`storage.base.Repository`, а не `SqliteRepository`. До этой правки §4.2
обещала, что `PostgresRepository` не потребует изменений в конвейере, а
`mypy --strict` — часть ворот проекта — отвергал любую альтернативную
реализацию: аннотация называла конкретный класс. Проверено исполнением,
см. `tests/test_pipeline.py::test_run_once_accepts_any_repository`.

Модуль разбит на файлы по шагам, а `run_once` здесь оставлен один и
целиком: единственное, что он знает, — ПОРЯДОК шагов и то, что журнал
прогона закрывается при любом исходе. Порядок здесь — не оформление:
сохранение идёт до отправки, отправка выбирает из базы, а не из памяти
(поэтому авария между шагами ничего не теряет), и пересчёт оценки стоит
между двумя чтениями `unreported()`, потому что карантин срабатывает
внутри чтения (см. reporting.py).

`build_sinks` вызывается ВНЕ этой функции и до неё — контракт задачи 9:
опечатка в имени приёмника обязана ронять процесс на старте, а не в
середине прогона, когда за страницы уже заплачено запросами к hh.ru.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.llm.client import OllamaClient
from hh_search.pipeline.discovery import discover, prefilter
from hh_search.pipeline.enrichment import enrich
from hh_search.pipeline.forbidden import ForbiddenStreak
from hh_search.pipeline.llm_enrich import (
    ReportEnrichment,
    build_ranker,
    embed_pending,
    extract_pending,
)
from hh_search.pipeline.reporting import report
from hh_search.pipeline.stats import EXIT_CODES, FAILED, OK, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.base import Repository

__all__ = ["EXIT_CODES", "FAILED", "OK", "PARTIAL", "RunStats", "run_once"]

logger = logging.getLogger(__name__)


def run_once(
    config: Config,
    client: PoliteClient,
    repo: Repository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    now: datetime | None = None,
    llm: OllamaClient | None = None,
) -> RunStats:
    """Один прогон целиком. Возвращает счётчики и статус, не бросает при частичном отказе.

    Наружу летит только `AccessForbidden` (спека §9: устойчивый 403 —
    остановка прогона) и ошибки программиста. Журнал прогона закрывается
    в любом случае, иначе в таблице `run` копятся строки `running`, и
    healthcheck перестаёт понимать, что происходит.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        # Наивная дата разъехалась бы с `reported_at`: имя файла отчёта
        # берётся отсюда, а `reported_at` пишется в UTC хранилищем — при
        # ночном прогоне это разные сутки.
        raise ValueError(f"момент прогона обязан быть aware UTC, получено {moment!r}")
    stats = RunStats()
    # Один счётчик на весь прогон: 403 на листинге и 403 на странице
    # вакансии — это два подряд идущих отказа доступа, а не по одному в
    # двух независимых местах.
    forbidden = ForbiddenStreak()
    run_id = repo.start_run()
    try:
        discover(config, client, repo, stats, forbidden)
        prefilter(config, repo, stats)
        enrich(config, client, repo, scorer, stats, forbidden)
        enrichment = _enrich_with_llm(config, repo, llm)
        report(repo, scorer, sinks, stats, moment, config.app.limits.rows_per_batch, enrichment)
    except AccessForbidden as error:
        stats.degrade(FAILED, f"hh.ru закрыл доступ: {error}")
        # Без «прогон остановлен» и без «обходные пути» — то и другое
        # уже сказано самим текстом исключения, и склейка давала
        # «…не применяются.. Обходные пути не применяются».
        logger.error("доступ к hh.ru закрыт: %s", error)
        _finish(repo, run_id, stats)
        raise
    except Exception as error:
        stats.degrade(FAILED, f"необработанная ошибка: {error}")
        logger.exception("прогон прерван необработанной ошибкой")
        _finish(repo, run_id, stats)
        raise
    _finish(repo, run_id, stats)
    logger.info(
        "прогон %s: найдено %d, новых %d, отсеяно %d, возвращено %d, обогащено %d, "
        "пересчитано %d, без оценки %d, застряло %d, в карантине %d, отправлено %d%s",
        stats.status,
        stats.discovered,
        stats.new_count,
        stats.rejected,
        stats.requeued,
        stats.enriched,
        stats.rescored,
        stats.stuck,
        stats.stalled,
        stats.corrupted,
        stats.reported,
        f", причина: {stats.error}" if stats.error else "",
    )
    return stats


def _enrich_with_llm(
    config: Config, repo: Repository, llm: OllamaClient | None
) -> ReportEnrichment | None:
    """Досчитать векторы и факты, вернуть то, чем дополнится отчёт.

    Стоит между `enrich` и `report`: описание уже скачано, а порядок ещё
    не понадобился. Ни один путь отсюда не бросает — все вызовы внутри
    гасят `LlmUnavailable` сами (§4 спеки
    `docs/superpowers/specs/2026-08-26-local-llm-design.md`), и это не
    вопрос вкуса: модель живёт на рабочей машине владельца, которая
    выключается на ночь, а прогон идёт раз в четыре часа.

    Два флага независимы: владелец, которому нужен только порядок выдачи,
    не платит пяти минут прогона за факты (§0.5, §7).
    """
    if llm is None:
        return None
    limit = config.app.limits.llm_per_run
    ranker = None
    if config.app.llm.semantic:
        embed_pending(llm, repo, config.app.llm.embed_model, limit)
        ranker = build_ranker(llm, config.profile, config.app.llm.embed_model)
    facts_model = None
    if config.app.llm.facts:
        extract_pending(
            llm,
            repo,
            config.app.llm.chat_model,
            limit,
            config.profile,
            config.profile.report_threshold,
        )
        facts_model = config.app.llm.chat_model
    if ranker is None and facts_model is None:
        return None
    return ReportEnrichment(ranker=ranker, facts_model=facts_model)


def _finish(repo: Repository, run_id: int, stats: RunStats) -> None:
    """Досчитать карантин и закрыть строку журнала — на ЛЮБОМ исходе.

    Карантин срабатывает внутри выборок, поэтому узнать о нём можно
    только у хранилища и только после того, как шаги отработали. Потеря
    вакансии навсегда до этого не отражалась ничем: изоляция порчи
    работала (одна битая строка не роняла остальные), а наблюдаемости у
    неё не было — прогон рапортовал `ok`, `error = NULL` и код 0. Для
    очереди пересчёта счётчики `rescored`/`stuck` заведены ровно ради
    этого; здесь симметричный `corrupted`.

    Понижение до `partial`, а не до `failed`: остальные вакансии прогон
    честно обработал и отправил, потеряна часть. Именно это `partial` и
    означает.
    """
    stats.corrupted = repo.corrupted_count()
    if stats.corrupted:
        stats.degrade(PARTIAL, f"{stats.corrupted} вакансий уведены в карантин безвозвратно")
        logger.error(
            "%d вакансий уведены в карантин со статусом corrupt и не попадут ни в один "
            "отчёт: испорчены данные, которых нет на странице вакансии, — перекачка их не "
            "восстановит (подробности выше). Значения оставлены в базе как улики",
            stats.corrupted,
        )
    repo.finish_run(run_id, stats.status, **stats.counters())
