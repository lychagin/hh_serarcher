"""Ручная уборка: план и исполнение (спека 2026-08-01 §3).

Оркестровка и только она. SQL живёт в `storage/retention.py`, отбор
файлов отчётов (форма имени, защищённое окно довозки) — в
`report_files.py`, вынесенном оттуда же ради бюджета строк (ревью
Task 4, раунд починки 1). `PROTECTED_DAYS` реэкспортируется отсюда: он
часть публичного интерфейса этого модуля (см. `tests/test_cleanup.py`),
хотя вычисляется в `report_files.py` — там же, где используется, рядом
с `LOOKBACK_DAYS` из `sinks/telegram_sink.py`, импортом константы, а не
переписанным числом: разъедься они — и уборка начала бы ломать довозку
молча.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

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

_MB = 1024 * 1024


@dataclass(frozen=True)
class CleanupDays:
    """Сроки хранения. `reports=None` означает «файлы не трогать вовсе».

    Одним полем выражены и флаг `--reports`, и его срок: два поля
    разъехались бы при первой же правке CLI.
    """

    descriptions: int = 90
    runs: int = 365
    reports: int | None = None

    def __post_init__(self) -> None:
        """Отрицательный срок отвергается, ноль — разрешён.

        Отрицательный срок задаёт границу В БУДУЩЕМ (`now -
        timedelta(days=-N)`), и уборка обнулила бы описание вакансии,
        отправленной секунду назад, — между `--descriptions-days -365` и
        «стереть всё» не стояло бы ничего. Ноль не отвергается: это
        предельно короткий, но осмысленный срок (`CleanupDays(reports=0)`
        — законное ручное действие, проверяемое сторожем окна довозки).
        Проверка живёт здесь, а не в CLI, — так её получит любой
        вызывающий, а не только команда.
        """
        for name, value in (
            ("descriptions", self.descriptions),
            ("runs", self.runs),
            ("reports", self.reports),
        ):
            if value is not None and value < 0:
                raise ValueError(f"срок хранения {name} не может быть отрицательным: {value}")


@dataclass(frozen=True)
class CleanupPlan:
    """Что уборка сделает или сделала. Одна форма на оба случая.

    Одна, а не две: сухой прогон обязан печатать РОВНО то, что напечатал
    бы `--apply`, иначе он перестаёт быть предпросмотром.
    """

    descriptions: int
    description_bytes: int
    runs: int
    report_files: int
    report_bytes: int
    descriptions_cutoff: datetime
    # Причины, по которым часть уборки не удалась (недоступный каталог
    # отчётов, гонка при удалении файла, отказ записи горизонта). Пустой
    # кортеж — уборка прошла без сучка. Поле обязано жить В ВОЗВРАЩЁННОМ
    # значении, а не только в логе (ревью Task 4, общее требование раунда
    # 1): человек не имеет права остаться в неведении, случилась ли
    # уборка целиком; текст переиспользует CLI будущей задачи.
    errors: tuple[str, ...] = ()

    def describe(self, applied: bool) -> str:
        verb = "убрано" if applied else "будет убрано"
        lines = [
            f"описаний: {self.descriptions} ({self.description_bytes / _MB:.1f} МБ) — {verb}",
            f"строк журнала прогонов: {self.runs} — {verb}",
            f"файлов отчётов: {self.report_files} ({self.report_bytes / _MB:.1f} МБ) — {verb}",
            f"граница хранения описаний: {self.descriptions_cutoff:%Y-%m-%d}",
        ]
        if applied:
            lines.append(
                "база ужата (VACUUM). `report --since` за границу описаний "
                "больше не покажет вакансий — предупреждение об этом печатает сам `report`"
            )
        for error in self.errors:
            lines.append(f"ОШИБКА: {error}")
        return "\n".join(lines)


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
        errors=(*dir_errors, *size_errors),
    )


def execute(
    repo: Housekeeper, reports_dir: Path, state_dir: Path, now: datetime, days: CleanupDays
) -> CleanupPlan:
    """Убрать и вернуть то, что убрано.

    Порядок: сначала файлы, потом база, потом `VACUUM`, потом горизонт.
    Горизонт последним потому, что он — обещание «за этой датой описаний
    нет», и записывать его до того, как они действительно убраны, значило
    бы обещать за уборку, которая могла и не случиться (сторож —
    `test_horizon_is_written_only_after_the_cleanup_it_promises`).

    Отказ любой части (каталог отчётов недоступен, файл исчез гонкой
    между `iterdir()` и `unlink()`, горизонт не записался) не поднимается
    исключением: уборка обязана довести остальное до конца и назвать
    отказ словами в `CleanupPlan.errors`, а не оставить человека гадать,
    случилась ли она хоть частично (ревью Task 4, I-1..I-3).
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
    _, size_before = repo.descriptions_before(cutoff)
    descriptions = repo.forget_descriptions(cutoff)
    runs = repo.forget_runs(now - timedelta(days=days.runs))
    repo.vacuum()
    horizon_error = _write_horizon(state_dir, cutoff, now)
    errors = (*dir_errors, *file_errors, *((horizon_error,) if horizon_error else ()))
    return CleanupPlan(
        descriptions=descriptions,
        description_bytes=size_before,
        runs=runs,
        report_files=removed,
        report_bytes=removed_bytes,
        descriptions_cutoff=cutoff,
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
