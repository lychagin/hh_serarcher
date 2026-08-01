"""Ручная уборка: план и исполнение (спека 2026-08-01 §3).

Оркестровка и только она. SQL живёт в `storage/retention.py`, отбор
файлов отчётов (форма имени, защищённое окно довозки) — в
`report_files.py`, а формы данных (`CleanupDays`, `CleanupPlan`) — в
`cleanup_plan.py`, вынесенных оттуда же ради бюджета строк (ревью Task 4,
раунд починки 1; ревью Task 5, раунд починки 1). Обе константы и оба
класса реэкспортируются отсюда: они часть публичного интерфейса этого
модуля (см. `tests/test_cleanup.py`), хотя вычисляются в своих файлах —
`PROTECTED_DAYS` там же, где используется, рядом с `LOOKBACK_DAYS` из
`sinks/telegram_sink.py`, импортом константы, а не переписанным числом:
разъедься они — и уборка начала бы ломать довозку молча.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.pipeline.cleanup_plan import CleanupDays, CleanupPlan
from hh_search.pipeline.report_files import PROTECTED_DAYS, total_bytes, victim_files
from hh_search.storage.base import Housekeeper

__all__ = [
    "PROTECTED_DAYS",
    "CleanupDays",
    "CleanupPlan",
    "execute",
    "horizon",
    "plan",
]

logger = logging.getLogger(__name__)

HORIZON_FILE = "last-cleanup"

# Насколько сохранённый горизонт может опережать НОВЫЙ `now`, оставаясь
# правдоподобным (ревью Task 4, раунд починки 3, Important-2) — тот же
# приём, что `CLOCK_SKEW_TOLERANCE` в `storage/run_log.py`. `execute()`
# получает `now` аргументом, и ничто не гарантирует, что между вызовами
# он растёт: коррекция NTP или ручная правка времени хоста может отвести
# часы назад, и тогда честно записанный вчера горизонт выглядит «из
# будущего» относительно нового `now`. Сутки — щедрый допуск для такого
# дрожания и одновременно достаточно узкий, чтобы отличить его от порчи:
# правдоподобная порча диска в валидную дату почти всегда даёт скачок на
# годы, а не на завтра. Часы, отведённые назад ДАЛЬШЕ этого допуска, эту
# защиту переживут, и горизонт всё равно съедет назад — тот же предел, в
# котором живёт вся остальная работа проекта с датами; не прячем его.
HORIZON_CLOCK_SKEW_TOLERANCE = timedelta(days=1)


def plan(repo: Housekeeper, reports_dir: Path, now: datetime, days: CleanupDays) -> CleanupPlan:
    """Посчитать, ничего не меняя."""
    victims, dir_errors = victim_files(reports_dir, now, days.reports)
    report_bytes, size_errors = total_bytes(victims)
    rows, size = repo.descriptions_before(now - timedelta(days=days.descriptions))
    return CleanupPlan(
        descriptions=rows,
        description_bytes=size,
        runs=repo.count_runs_before(now - timedelta(days=days.runs)),
        report_files=len(victims),
        report_bytes=report_bytes,
        descriptions_cutoff=now - timedelta(days=days.descriptions),
        reports_considered=days.reports is not None,
        errors=(*dir_errors, *size_errors),
    )


def execute(
    repo: Housekeeper, reports_dir: Path, state_dir: Path, now: datetime, days: CleanupDays
) -> CleanupPlan:
    """Убрать и вернуть то, что убрано.

    Порядок: сначала файлы, потом описания, потом журнал прогонов, потом
    `VACUUM`, потом горизонт. Каждый шаг с базой пойман ОТДЕЛЬНО (ревью
    Task 5, раунд починки 1, Important-1): раньше исключение из любого из
    трёх вызовов репозитория улетало наружу мимо `CleanupPlan.errors`,
    прямиком в `_storage_errors` CLI, — и `describe()` не печатался вовсе,
    хотя файлы уже были удалены, а часть базы уже изменена. Замер ревью:
    `vacuum()`, упавший `database or disk is full` (самый вероятный отказ
    именно для VACUUM — ему нужна вторая копия базы на диске), прятал 5
    удалённых файлов, 3 обнулённых описания и 2 удалённые строки журнала.

    Горизонт пишется, только если описания ДЕЙСТВИТЕЛЬНО обнулились
    (`descriptions_ok`): он обещает «за этой датой описаний нет», а
    писать его после отказа `forget_descriptions` значило бы обещать за
    уборку, которой не было, — инвариант Task 4
    (`test_horizon_is_written_only_after_the_cleanup_it_promises`), теперь
    исполняемый явной проверкой, а не побочным эффектом порядка вызовов.
    Отказ `forget_runs` или `vacuum` горизонт не блокирует: они не имеют
    отношения к тому, что он обещает, — если бы блокировали, честно
    убранные описания остались бы без предупреждения `report --since`
    навсегда, то есть отказ VACUUM убивал бы ровно тот механизм, ради
    которого горизонт заведён.

    Отказ любой части (каталог отчётов недоступен, файл исчез гонкой
    между `iterdir()` и `unlink()`, шаг базы, горизонт не записался) не
    поднимается исключением: уборка обязана довести остальное до конца и
    назвать отказ словами в `CleanupPlan.errors`, а не оставить человека
    гадать, случилась ли она хоть частично (ревью Task 4, I-1..I-3; ревью
    Task 5, Important-1).
    """
    victims, dir_errors = victim_files(reports_dir, now, days.reports)
    removed_bytes = 0
    removed = 0
    file_errors: list[str] = []
    for path in victims:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as error:
            logger.error("файл отчёта %s не удалён: %s", path, error)
            file_errors.append(f"файл отчёта {path} не удалён: {error}")
            continue
        removed += 1
        removed_bytes += size

    cutoff = now - timedelta(days=days.descriptions)
    descriptions = 0
    size_before = 0
    descriptions_ok = False
    storage_errors: list[str] = []
    try:
        _, size_before = repo.descriptions_before(cutoff)
        descriptions = repo.forget_descriptions(cutoff)
        descriptions_ok = True
    except Exception as error:  # noqa: BLE001 — отказ базы не имеет права
        # прятать уже удалённые выше файлы; репозиторий не обещает
        # конкретной иерархии исключений (см. `Housekeeper`), и ловится
        # ЛЮБОЙ отказ, а не только известный сегодня.
        message = f"описания не обнулены: {error}"
        logger.error(message)
        storage_errors.append(message)

    runs = 0
    try:
        runs = repo.forget_runs(now - timedelta(days=days.runs))
    except Exception as error:  # noqa: BLE001 — своя ошибка не должна
        # прятать уже убранные файлы и описания.
        message = f"журнал прогонов не убран: {error}"
        logger.error(message)
        storage_errors.append(message)

    vacuum_ok = False
    try:
        repo.vacuum()
        vacuum_ok = True
    except Exception as error:  # noqa: BLE001 — VACUUM переписывает базу
        # целиком и держит вторую копию на диске — самый вероятный отказ
        # именно здесь; не отменяет уже обнулённые описания и удалённые
        # строки журнала.
        message = f"база не ужата (VACUUM): {error}"
        logger.error(message)
        storage_errors.append(message)

    horizon_error = _write_horizon(state_dir, cutoff, now) if descriptions_ok else None
    errors = (
        *dir_errors,
        *file_errors,
        *storage_errors,
        *((horizon_error,) if horizon_error else ()),
    )
    return CleanupPlan(
        descriptions=descriptions,
        description_bytes=size_before,
        runs=runs,
        report_files=removed,
        report_bytes=removed_bytes,
        descriptions_cutoff=cutoff,
        reports_considered=days.reports is not None,
        vacuum_ok=vacuum_ok,
        errors=errors,
    )


def horizon(state_dir: Path) -> date | None:
    """Граница последней уборки описаний или `None`, если её не было.

    Нечитаемый файл — тоже `None`: предупреждение, роняющее команду
    `report`, хуже отсутствующего предупреждения.
    """
    try:
        return date.fromisoformat((state_dir / HORIZON_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_horizon(state_dir: Path, cutoff: datetime, now: datetime) -> str | None:
    """Записать горизонт МОНОТОННО; отказ возвращается текстом, не исключением.

    Монотонность — только вперёд, `max(старое, новое)` (ревью Task 4,
    I-4): более мягкий срок хранения в следующем прогоне не имеет права
    отменить обещание, данное более жёстким прогоном раньше, иначе «за
    этой датой описаний нет» стало бы ложью задним числом.

    Сохранённый горизонт, ушедший вперёд от `now` дальше
    `HORIZON_CLOCK_SKEW_TOLERANCE`, доверия не заслуживает и в `max` не
    участвует (ревью Task 4, раунд починки 2 — регрессия самой I-4).
    Горизонт по построению — это `now - неотрицательный срок`
    (`CleanupDays.__post_init__` отвергает отрицательный срок), то есть
    честно посчитанное значение почти никогда не опережает `now`; такой
    разрыв — порча, ручная правка или баг где-то ещё, и `horizon()` его
    не перехватывает: дата синтаксически валидна. Без этой защиты
    `max(будущее, что угодно)` залипал бы на будущей дате навсегда — ни
    один следующий честный прогон её не перекрыл бы, а `report --since`
    предупреждало бы ПОСТОЯННО, то есть точно так же бесполезно, как не
    предупреждало бы никогда (тот же класс обесцененного сигнала, что
    R-3 чинит для `partial`).

    Допуск, а не голое `existing <= now.date()` (раунд починки 3,
    Important-2), — потому что `now` между вызовами не гарантированно
    растёт: небольшой откат часов назад иначе делал бы вчерашний честный
    горизонт неотличимым от порчи и откатывал его на более раннюю дату,
    ломая ту самую монотонность, ради которой всё это писалось. Цена
    ошибки асимметрична: горизонт, откатившийся назад, заставляет
    `report --since` НЕДОпредупреждать (человек тихо получает неполную
    выборку) — это и есть потеря, ради которой горизонт заведён; горизонт,
    оставшийся более новым, чем следовало бы, самое большее лишний раз
    предупреждает — шум, не потеря. При неопределённости решение — в
    пользу более поздней даты. Часы, отведённые назад ДАЛЬШЕ допуска,
    эту защиту переживут, и горизонт всё равно съедет назад — предел
    назван у `HORIZON_CLOCK_SKEW_TOLERANCE`, не спрятан здесь.

    Запись стоит ПОСЛЕ необратимого шага уборки (файлы удалены, база
    ужата), поэтому её отказ не поднимается исключением (I-3): человек
    обязан узнать, что уборка случилась, а обещание записать не удалось,
    а не получить трейсбек без единой подсказки, что произошло на самом
    деле.
    """
    new_day = cutoff.date()
    existing = horizon(state_dir)
    if existing is not None and existing <= now.date() + HORIZON_CLOCK_SKEW_TOLERANCE:
        day = max(existing, new_day)
    else:
        day = new_day
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / HORIZON_FILE).write_text(f"{day:%Y-%m-%d}\n", encoding="utf-8")
    except OSError as error:
        message = f"горизонт не записан ({state_dir / HORIZON_FILE}): {error}"
        logger.error(message)
        return message
    return None
