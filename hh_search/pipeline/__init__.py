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
from hh_search.pipeline.forbidden import ForbiddenStreak
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
    # Один счётчик на весь прогон: 403 на листинге и 403 на странице
    # вакансии — это два подряд идущих отказа доступа, а не по одному в
    # двух независимых местах.
    forbidden = ForbiddenStreak()
    run_id = repo.start_run()
    try:
        discover(config, client, repo, stats, forbidden)
        prefilter(config, repo, stats)
        enrich(config, client, repo, scorer, stats, forbidden)
        report(repo, scorer, sinks, stats, moment)
    except AccessForbidden as error:
        stats.degrade(FAILED, f"hh.ru закрыл доступ: {error}")
        logger.error("прогон остановлен: %s. Обходные пути не применяются", error)
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


def _finish(repo: SqliteRepository, run_id: int, stats: RunStats) -> None:
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
