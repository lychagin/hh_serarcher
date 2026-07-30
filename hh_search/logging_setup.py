"""Логи одновременно в stdout (их забирает `docker logs`) и в файл с ротацией."""

import contextlib
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# httpx на INFO пишет строку на КАЖДЫЙ запрос, включая robots.txt. За сутки
# это несколько сотен строк, среди которых теряются наши ERROR — а именно
# они здесь единственный способ узнать о потере данных.
#
# Это ещё и защита секрета, а не только борьба с шумом: в строке httpx
# лежит полный URL, а у Bot API токен стоит в ПУТИ URL
# (`/bot<ТОКЕН>/sendMessage`). Второй, независимой защитой служит
# `_TokenFilter` из `sinks/telegram_client.py`, и он висит на обработчиках
# корня — потому что глушилка ниже снимается одной строкой в чужой отладке
# (`logging.getLogger("httpx").setLevel(logging.INFO)`), а защита пароля
# бота не имеет права зависеть от чужого уровня логирования.
QUIET_LOGGERS = ("httpx", "httpcore")


class ResilientFileHandler(RotatingFileHandler):
    """Файловый обработчик, который умеет ОТКАЗАТЬ, а не сыпать трейсбеками.

    `setup_logging` проверяет каталог логов один раз, при старте, и этого
    достаточно ровно до первой смены прав на ходу. Дальше `doRollover`
    бросает `PermissionError` на КАЖДОЙ записи, `logging` исключение
    подавляет и печатает полный traceback в stderr: замер — 830 байт
    мусора на запись, все записи в файл потеряны, процесс жив и выглядит
    здоровым. В докере stderr уходит в `docker logs`, где нашей ротации
    уже нет, то есть беда лечится обратной стороной той же беды.

    Поэтому первый же отказ файла выключает обработчик насовсем и
    объявляет об этом ОДИН раз. Насовсем — потому что причина у отказа
    записи в файл всегда одна и та же (права, `:ro`, полный диск) и сама
    не проходит, а сервис обязан продолжать искать вакансии: stdout
    остаётся, и в нём остаётся всё, что было бы в файле.
    """

    def __init__(
        self, filename: Path | str, *, max_bytes: int, backup_count: int, encoding: str
    ) -> None:
        super().__init__(
            str(filename), maxBytes=max_bytes, backupCount=backup_count, encoding=encoding
        )
        self.disabled_by: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if self.disabled_by is not None:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 — имя из logging
        """Вызывается самим `logging`, когда `emit` бросил исключение."""
        error = sys.exc_info()[1]
        if self.disabled_by is not None:
            return
        # Флаг взводится ДО жалобы: жалоба пойдёт через тот же корневой
        # логгер, то есть в том числе сюда, и без флага получилась бы
        # рекурсия на пустом месте.
        self.disabled_by = str(error) or type(error).__name__
        with contextlib.suppress(OSError):
            self.close()
        logging.getLogger(__name__).error(
            "файловый лог %s отключён после первой же ошибки записи (%s); дальше пишем "
            "только в stdout. Ротация не работает, и `docker logs` теперь единственный "
            "след — проверьте права на каталог логов и место на диске",
            self.baseFilename,
            self.disabled_by,
        )


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
            ResilientFileHandler(
                logs_dir / "hh.log", max_bytes=5_000_000, backup_count=5, encoding="utf-8"
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
