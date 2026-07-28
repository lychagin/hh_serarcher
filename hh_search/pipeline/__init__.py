"""Оркестрация семи шагов конвейера (спека §4.1).

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
from hh_search.pipeline.discovery import discover, prefilter
from hh_search.pipeline.enrichment import enrich
from hh_search.pipeline.reporting import report
from hh_search.pipeline.stats import EXIT_CODES, FAILED, OK, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

__all__ = ["EXIT_CODES", "FAILED", "OK", "PARTIAL", "RunStats", "run_once"]

logger = logging.getLogger(__name__)


def run_once(
    config: Config,
    client: PoliteClient,
    repo: SqliteRepository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    now: datetime | None = None,
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
    run_id = repo.start_run()
    try:
        discover(config, client, repo, stats)
        prefilter(config, repo, stats)
        enrich(config, client, repo, scorer, stats)
        report(repo, scorer, sinks, stats, moment)
    except AccessForbidden as error:
        stats.degrade(FAILED, f"hh.ru закрыл доступ: {error}")
        logger.error("прогон остановлен: %s. Обходные пути не применяются", error)
        repo.finish_run(run_id, stats.status, **stats.counters())
        raise
    except Exception as error:
        stats.degrade(FAILED, f"необработанная ошибка: {error}")
        logger.exception("прогон прерван необработанной ошибкой")
        repo.finish_run(run_id, stats.status, **stats.counters())
        raise
    repo.finish_run(run_id, stats.status, **stats.counters())
    logger.info(
        "прогон %s: найдено %d, новых %d, отсеяно %d, обогащено %d, пересчитано %d, "
        "без оценки %d, отправлено %d%s",
        stats.status,
        stats.discovered,
        stats.new_count,
        stats.rejected,
        stats.enriched,
        stats.rescored,
        stats.stuck,
        stats.reported,
        f", причина: {stats.error}" if stats.error else "",
    )
    return stats
