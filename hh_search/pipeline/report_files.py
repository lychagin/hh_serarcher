"""Отбор файлов отчётов под удаление — часть ручной уборки.

Вынесено из `cleanup.py` (ревью Task 4, раунд починки 1): защита от
недоступного каталога и от гонки при чтении размера (I-1, I-2) добавила
оркестровке достаточно строк, чтобы файл перешагнул бюджет 150 строк
кода. Файл делится, а не получает строку-исключение в §4.3 спеки —
решение владельца, принятое трижды (`CLAUDE.md`). Здесь — только то, что
знает форму имени файла отчёта и защищённое окно довозки; SQL и порядок
шагов уборки остаются в `retention.py` и `cleanup.py`.
"""

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.sinks.telegram_sink import LOOKBACK_DAYS

logger = logging.getLogger(__name__)

# Имя файла отчёта начинается с даты: `2026-07-31-new.html`, `-new.csv`,
# `-new.md`, плюс отметка о доставке `-new.html.sent`. Уборка не трогает
# ничего, что не начинается с даты в этой форме, — включая черновики
# `*.part` (у них дата тоже впереди, но суффикс чужой). Критерий — форма
# ИМЕНИ, а не авторство: файл человека, чьё имя само начинается с даты
# (`2026-01-01-мои-заметки.txt`), под правило подпадает и удаляется
# наравне с отчётами (ревью Task 4, M-1).
_REPORT_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-[^/]*(?<!\.part)$")

# Сколько последних суток отчётов защищено в любом случае: окно довозки
# плюс сегодня. Один день сверх необходимого взят намеренно — цена
# лишнего файла на диске нулевая, цена удалённой отметки `.sent` —
# повторный документ в канале.
PROTECTED_DAYS = LOOKBACK_DAYS + 1


def victim_files(
    reports_dir: Path, now: datetime, reports_days: int | None
) -> tuple[list[Path], list[str]]:
    """Файлы отчётов под удаление и текст ошибок. Защищённое окно не отдаётся никогда.

    Недоступный каталог (нет прав, `reports_dir` оказался файлом) не
    роняет уборку целиком — тот же приём, что `TelegramSink.
    _sweep_orphaned_drafts` применяет к тому же каталогу отчётов:
    недоступный на запись каталог не имеет права ронять `maintain`.
    Отказ возвращается ТЕКСТОМ, а не молча глотается (ревью Task 4, I-1).
    """
    if reports_days is None or not reports_dir.exists():
        return [], []
    try:
        entries = sorted(reports_dir.iterdir())
    except OSError as error:
        message = f"каталог отчётов {reports_dir} недоступен: {error}"
        logger.error(message)
        return [], [message]
    cutoff = now.date() - timedelta(days=reports_days)
    protected_from = now.date() - timedelta(days=PROTECTED_DAYS)
    victims = []
    for path in entries:
        if not path.is_file():
            continue
        day = _day_of(path.name)
        if day is None or day >= cutoff or day >= protected_from:
            continue
        victims.append(path)
    return victims, []


def total_bytes(paths: list[Path]) -> tuple[int, list[str]]:
    """Сумма байт жертв для сухого прогона.

    Гонка (файл исчез между `iterdir()` и этим `stat()`) не роняет план
    (ревью Task 4, I-2/C2): пропавший файл просто не вносит байт, а
    причина уходит в возвращённый список ошибок, а не наружу исключением.
    """
    total = 0
    errors: list[str] = []
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError as error:
            errors.append(f"размер {path} не прочитан: {error}")
    return total, errors


def _day_of(name: str) -> date | None:
    match = _REPORT_NAME_RE.match(name)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
