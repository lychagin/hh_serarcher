"""Ручная уборка: план и исполнение (спека 2026-08-01 §3).

Оркестровка и только она. SQL живёт в `storage/retention.py`, а знание о
том, каких файлов отчётов касаться нельзя, взято из
`sinks/telegram_sink.py` ИМПОРТОМ константы, а не переписанным числом:
разъедься они — и уборка начала бы ломать довозку молча.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.sinks.telegram_sink import LOOKBACK_DAYS
from hh_search.storage.base import Housekeeper

logger = logging.getLogger(__name__)

# Имя файла отчёта начинается с даты: `2026-07-31-new.html`, `-new.csv`,
# `-new.md`, плюс отметка о доставке `-new.html.sent`. Всё, что под
# образец не подпадает, уборка не трогает — включая черновики `*.part`
# (у них дата тоже впереди, но суффикс чужой) и любые файлы человека.
_REPORT_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-[^/]*(?<!\.part)$")

# Сколько последних суток отчётов защищено в любом случае: окно довозки
# плюс сегодня. Один день сверх необходимого взят намеренно — цена
# лишнего файла на диске нулевая, цена удалённой отметки `.sent` —
# повторный документ в канале.
PROTECTED_DAYS = LOOKBACK_DAYS + 1

HORIZON_FILE = "last-cleanup"

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
        return "\n".join(lines)


def plan(repo: Housekeeper, reports_dir: Path, now: datetime, days: CleanupDays) -> CleanupPlan:
    """Посчитать, ничего не меняя."""
    victims = _victim_files(reports_dir, now, days)
    rows, size = repo.descriptions_before(now - timedelta(days=days.descriptions))
    return CleanupPlan(
        descriptions=rows,
        description_bytes=size,
        runs=repo.count_runs_before(now - timedelta(days=days.runs)),
        report_files=len(victims),
        report_bytes=sum(path.stat().st_size for path in victims),
        descriptions_cutoff=now - timedelta(days=days.descriptions),
    )


def execute(
    repo: Housekeeper, reports_dir: Path, state_dir: Path, now: datetime, days: CleanupDays
) -> CleanupPlan:
    """Убрать и вернуть то, что убрано.

    Порядок: сначала файлы, потом база, потом `VACUUM`, потом горизонт.
    Горизонт последним потому, что он — обещание «за этой датой описаний
    нет», и записывать его до того, как они действительно убраны, значило
    бы обещать за уборку, которая могла и не случиться.
    """
    victims = _victim_files(reports_dir, now, days)
    removed_bytes = 0
    removed = 0
    for path in victims:
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as error:
            logger.error("файл отчёта %s не удалён: %s", path, error)
            continue
        removed += 1
        removed_bytes += size
    cutoff = now - timedelta(days=days.descriptions)
    _, size_before = repo.descriptions_before(cutoff)
    descriptions = repo.forget_descriptions(cutoff)
    runs = repo.forget_runs(now - timedelta(days=days.runs))
    repo.vacuum()
    _write_horizon(state_dir, cutoff)
    return CleanupPlan(
        descriptions=descriptions,
        description_bytes=size_before,
        runs=runs,
        report_files=removed,
        report_bytes=removed_bytes,
        descriptions_cutoff=cutoff,
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


def _write_horizon(state_dir: Path, cutoff: datetime) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / HORIZON_FILE).write_text(f"{cutoff:%Y-%m-%d}\n", encoding="utf-8")


def _victim_files(reports_dir: Path, now: datetime, days: CleanupDays) -> list[Path]:
    """Файлы отчётов под удаление. Защищённое окно не отдаётся никогда."""
    if days.reports is None or not reports_dir.exists():
        return []
    cutoff = now.date() - timedelta(days=days.reports)
    protected_from = now.date() - timedelta(days=PROTECTED_DAYS)
    victims = []
    for path in sorted(reports_dir.iterdir()):
        if not path.is_file():
            continue
        day = _day_of(path.name)
        if day is None or day >= cutoff or day >= protected_from:
            continue
        victims.append(path)
    return victims


def _day_of(name: str) -> date | None:
    match = _REPORT_NAME_RE.match(name)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
