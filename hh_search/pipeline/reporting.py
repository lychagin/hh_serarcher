"""Шаг 7: отправка готового в приёмники (спека §4.1, §5.2).

Порядок здесь важнее кода. Карантин срабатывает ВНУТРИ `unreported()`:
нечитаемая оценка обнуляется именно в момент чтения, и вакансия уходит в
`pending_scoring`. Значит один вызов `unreported()` не может вернуть то,
что он же только что отправил на пересчёт, — а лечение обязано занимать
один прогон, не два. Отсюда два прохода `пересчёт → unreported()`:

* первый разгребает очередь, которую мог создать сам шаг обогащения
  (`save_description` при отказе оценки), и читает готовое;
* второй досчитывает то, что карантин обнулил во время этого чтения.

Двух проходов ДОСТАТОЧНО и это доказуемо: записать оценку, которая не
читается обратно, нельзя — `ScoreBreakdown` запрещает `inf`/`nan` на
входе, поэтому пересчитанная оценка не может снова уйти в карантин.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from hh_search.domain.models import ScoredVacancy
from hh_search.pipeline.enrichment import rescore
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)

_PASSES = 2


def report(
    repo: SqliteRepository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    stats: RunStats,
    moment: datetime,
) -> None:
    ready = _collect(repo, scorer, stats)
    if not ready:
        return
    if not sinks:
        # Недостижимо через конфиг (`sinks` требует min_length=1), но
        # пометить вакансии отправленными, не отправив их никуда, — тихая
        # потеря, а такие пути в этом проекте обязаны кричать.
        stats.degrade(FAILED, "приёмников нет, отправлять некуда")
        logger.error("приёмников нет: %d вакансий остаются в очереди отправки", len(ready))
        return
    delivered, failed = _emit(sinks, ready, moment)
    if failed:
        _complain(ready, delivered, failed, stats)
        return
    repo.mark_reported([item.discovered.id for item in ready])
    stats.reported = len(ready)
    logger.info("отправлено вакансий: %d, приёмники: %s", len(ready), ", ".join(delivered))


def _collect(repo: SqliteRepository, scorer: Scorer, stats: RunStats) -> list[ScoredVacancy]:
    """Готовое к отправке — после того, как очередь пересчёта разобрана."""
    ready: list[ScoredVacancy] = []
    for _ in range(_PASSES):
        stats.rescored += rescore(repo, scorer, stats)
        ready = repo.unreported()
    # Хранилище кричит из `unreported()` о застрявших строках, но пропуск
    # шага не должен быть тихим и здесь: `stuck` уезжает в журнал прогона,
    # а id — в лог, потому что без них не понять, какие вакансии, уже
    # стоившие запроса к hh.ru, не попадут ни в один отчёт.
    stuck = repo.pending_scoring()
    stats.stuck = len(stuck)
    if stuck:
        stats.degrade(PARTIAL, f"оценка не досчитана у {len(stuck)} вакансий")
        logger.error(
            "%d вакансий с готовым описанием остались без оценки и не попадут в отчёт: %s. "
            "Описание у них есть, перекачка не нужна — нужен локальный пересчёт",
            len(stuck),
            ", ".join(vacancy.id for vacancy, _ in stuck),
        )
    return ready


def _emit(
    sinks: Sequence[Sink], ready: Sequence[ScoredVacancy], moment: datetime
) -> tuple[list[str], list[str]]:
    delivered: list[str] = []
    failed: list[str] = []
    for sink in sinks:
        try:
            sink.emit(ready, moment)
        except Exception as error:  # noqa: BLE001 — падение приёмника не теряет вакансии
            failed.append(sink.name)
            logger.error(
                "приёмник %s не принял %d вакансий: %s",
                sink.name,
                len(ready),
                error,
                exc_info=True,
            )
        else:
            delivered.append(sink.name)
    return delivered, failed


def _complain(
    ready: Sequence[ScoredVacancy], delivered: Sequence[str], failed: Sequence[str], stats: RunStats
) -> None:
    """Ни одной вакансии не помечаем отправленной — и говорим, чем платим.

    `mark_reported` только при успехе ВСЕХ приёмников защищает от потери,
    но не от повтора: следующий прогон отдаст те же вакансии заново, и
    приёмник, отработавший сейчас, увидит их второй раз. Устранить это
    внутри конвейера нечем — доставка at-least-once по построению, —
    поэтому идемпотентность по `id` остаётся обязанностью приёмника, а
    здесь обязателен громкий лог и понижение статуса: молча задваивать
    отчёт нельзя.
    """
    stats.degrade(PARTIAL, f"приёмники не приняли отчёт: {', '.join(failed)}")
    logger.error(
        "приёмники %s не приняли отчёт, поэтому %d вакансий остаются в очереди отправки. "
        "Следующий прогон отправит их заново, и приёмники, отработавшие сейчас (%s), "
        "увидят их повторно",
        ", ".join(failed),
        len(ready),
        ", ".join(delivered) or "ни один",
    )
