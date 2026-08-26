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
from hh_search.pipeline.llm_enrich import SemanticRanker
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.storage.base import Repository

logger = logging.getLogger(__name__)

_PASSES = 2


def report(
    repo: Repository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    stats: RunStats,
    moment: datetime,
    limit: int,
    ranker: SemanticRanker | None = None,
) -> None:
    # ДО раннего возврата при пустой очереди и до `emit`: обслуживание не
    # зависит от наличия работы (T-2), а довезённый вчерашний документ
    # обязан лечь в канале раньше сегодняшнего.
    maintain_sinks(sinks, moment)
    ready = _collect(repo, scorer, stats, limit)
    if not ready:
        return
    # Семантика проставляется ЗДЕСЬ, а не в `_collect`: она не влияет на
    # то, что попадёт в отчёт, только на порядок внутри него. Отсутствие
    # ранжировщика (модель недоступна, `llm.semantic: false`, команда
    # `report` без LLM) оставляет `semantic` пустым у всех, и устойчивая
    # сортировка приёмников даёт ровно прежний порядок — §4 спеки
    # `docs/superpowers/specs/2026-08-26-local-llm-design.md`.
    if ranker is not None:
        ready = ranker.attach(repo, ready)
    if not sinks:
        # Недостижимо через конфиг (`sinks` требует min_length=1), но
        # пометить вакансии отправленными, не отправив их никуда, — тихая
        # потеря, а такие пути в этом проекте обязаны кричать.
        stats.degrade(FAILED, "приёмников нет, отправлять некуда")
        logger.error("приёмников нет: %d вакансий остаются в очереди отправки", len(ready))
        return
    written, failed = emit_to_sinks(sinks, ready, moment)
    if failed:
        _complain(ready, list(written), failed, stats)
        return
    repo.mark_reported([item.discovered.id for item in ready])
    stats.reported = len(ready)
    logger.info(
        "отправлено вакансий: %d, приёмники: %s",
        len(ready),
        ", ".join(f"{name} (записано {count})" for name, count in written.items()),
    )


def _collect(repo: Repository, scorer: Scorer, stats: RunStats, limit: int) -> list[ScoredVacancy]:
    """Готовое к отправке — после того, как очередь пересчёта разобрана."""
    ready: list[ScoredVacancy] = []
    for _ in range(_PASSES):
        stats.rescored += rescore(repo, scorer, stats, limit)
        ready = repo.unreported(limit)
    # Хранилище кричит из `unreported()` о застрявших строках, но пропуск
    # шага не должен быть тихим и здесь: `stuck` уезжает в журнал прогона,
    # а id — в лог, потому что без них не понять, какие вакансии, уже
    # стоившие запроса к hh.ru, не попадут ни в один отчёт.
    #
    # Считается COUNT'ом, а не длиной выборки: с появлением потолка
    # `len(pending_scoring(limit))` рапортовал бы ровно `limit` при любом
    # размере бэклога — счётчик, который перестаёт расти ровно там, где
    # беда становится большой. Имена по-прежнему берутся из выборки, но
    # ровно те, что в неё поместились, и сообщение об этом говорит.
    stuck = repo.count_pending_scoring()
    stats.stuck = stuck
    if stuck:
        stats.degrade(PARTIAL, f"оценка не досчитана у {stuck} вакансий")
        named = [vacancy.id for vacancy, _ in repo.pending_scoring(limit)]
        tail = f" и ещё {stuck - len(named)}" if stuck > len(named) else ""
        logger.error(
            "%d вакансий с готовым описанием остались без оценки и не попадут в отчёт: %s%s. "
            "Описание у них есть, перекачка не нужна — нужен локальный пересчёт; почему "
            "он не удался, сказано записями выше",
            stuck,
            ", ".join(named),
            tail,
        )
    return ready


def maintain_sinks(sinks: Sequence[Sink], moment: datetime) -> None:
    """Дать каждому приёмнику обслужиться. Отказ громкий, но не заразный.

    Статус прогона не понижается сознательно: недоступный Telegram иначе
    красил бы `partial` каждые четыре часа, обесценивая статус ровно так
    же, как это делала вырожденная страница листинга до R-3. Потеря при
    отказе ограничена: признак застрявшего документа — файл на диске, и
    следующий прогон попробует снова.
    """
    for sink in sinks:
        try:
            sink.maintain(moment)
        except Exception as error:  # noqa: BLE001 — обслуживание не роняет прогон
            logger.error(
                "приёмник %s не обслужен: %s. Статус прогона не понижен, "
                "следующий прогон попробует снова",
                sink.name,
                error,
                exc_info=True,
            )


def emit_to_sinks(
    sinks: Sequence[Sink], ready: Sequence[ScoredVacancy], moment: datetime
) -> tuple[dict[str, int], list[str]]:
    """Отдать вакансии всем приёмникам.

    Возвращает `({доставивший: сколько записал}, [отказавшие])`. Не просто
    список доставивших: приёмники дедуплицируют по файлу дня, и «отдали
    143» с «записали 143» совпадает далеко не всегда — а тот, кто читает
    вывод, решает по нему, искать ли ему отчёт на диске.

    Публична, потому что `report` в CLI обязан вести себя ровно так же:
    там та же самая недоступность каталога отчётов давала голый traceback,
    тогда как `run` в этой же ситуации отдаёт внятный текст и ненулевой
    код. Разное поведение двух команд на одном отказе — это не мелочь: по
    выводу `report` человек решает, чинить ему конфиг или том.
    """
    written: dict[str, int] = {}
    failed: list[str] = []
    for sink in sinks:
        try:
            count = sink.emit(ready, moment)
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
            written[sink.name] = count
    return written, failed


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

    Насколько понижать — зависит от того, дошло ли хоть что-нибудь.
    `partial` означает частичную потерю работы и считается успехом для
    `last_successful_run()`, то есть для healthcheck. Прогон, у которого
    очередь отправки не пуста, а доставлено НОЛЬ, работы не сделал вовсе:
    так выглядит том отчётов, смонтированный не туда, и так он выглядит
    сутками, пока healthcheck зелен. Поэтому здесь `failed`.

    Сужать вместо этого `last_successful_run()` до «`ok` или `partial` с
    `reported > 0`» было бы неверно: `partial` с нулём отправленных —
    штатный исход прогона, которому просто нечего отправлять (одна
    страница листинга не получена, новых готовых вакансий нет). Такой
    прогон стал бы красить индикатор в красный без всякой потери. Здесь
    же известно и то, и другое — и размер очереди, и число доставивших, —
    поэтому различение точное.
    """
    stats.degrade(
        PARTIAL if delivered else FAILED, f"приёмники не приняли отчёт: {', '.join(failed)}"
    )
    logger.error(
        "приёмники %s не приняли отчёт, поэтому %d вакансий остаются в очереди отправки. "
        "Следующий прогон отправит их заново, и приёмники, отработавшие сейчас (%s), "
        "увидят их повторно",
        ", ".join(failed),
        len(ready),
        ", ".join(delivered) or "ни один",
    )
