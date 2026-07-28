"""Логи одновременно в stdout (их забирает `docker logs`) и в файл с ротацией."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# httpx на INFO пишет строку на КАЖДЫЙ запрос, включая robots.txt. За сутки
# это несколько сотен строк, среди которых теряются наши ERROR — а именно
# они здесь единственный способ узнать о потере данных.
QUIET_LOGGERS = ("httpx", "httpcore")


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Настроить корневой логгер. Вызывается КАЖДОЙ командой CLI.

    Не только `run`/`serve`: карантин хранилища пишет `ERROR` из любой
    команды, включая `report` и `mark`, и эти записи — единственный след
    порчи данных. Уходить в никуда они не имеют права.
    """
    formatter = logging.Formatter(FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: OSError | None = None
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                logs_dir / "hh.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    except OSError as error:
        # Недоступный каталог логов — не причина не искать вакансии:
        # stdout остаётся, и в него же уходит жалоба на потерю файла.
        file_error = error

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        root.error("логи пишутся только в stdout: каталог %s недоступен (%s)", logs_dir, file_error)
