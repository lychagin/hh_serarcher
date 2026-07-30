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
# `TokenFilter` (ниже в этом модуле), и он висит на обработчиках корня —
# потому что глушилка ниже снимается одной строкой в чужой отладке
# (`logging.getLogger("httpx").setLevel(logging.INFO)`), а защита пароля
# бота не имеет права зависеть от чужого уровня логирования.
QUIET_LOGGERS = ("httpx", "httpcore")

# Секреты, зарегистрированные через `redact_secret`, — переживают порядок
# вызовов. `TelegramClient` вешал фильтр на обработчики корня САМ, из
# своего `__init__`, и это работало только потому, что в `__main__.py` он
# сегодня строится ПОСЛЕ `setup_logging`. Порядок нигде не закреплён:
# клиент, построенный раньше нее, добавил бы фильтр к обработчикам,
# которые `setup_logging` тут же заменит (`root.handlers.clear()`), — и
# токен тихо потёк бы в новые. Регистрация здесь избавляет защиту от этой
# зависимости: `redact_secret` применяет фильтр к обработчикам, какие есть
# СЕЙЧАС, а `setup_logging` — ко всем зарегистрированным секретам на
# КАЖДЫЕ новые обработчики, которые создаёт, в любом порядке вызовов.
_secrets: list[tuple[str, str]] = []


class TokenFilter(logging.Filter):
    """Вычищает секрет из записи, кто бы её ни сделал, — и из сообщения, и
    из уже отформатированного `exc_info`.

    Обе части чистятся отдельно, потому что формируются отдельно:
    `record.getMessage()` ничего не знает про `exc_info` — тот
    форматируется ЛЕНИВО, внутри `Formatter.format()`, и результат
    кешируется в `record.exc_text`. Фильтр из первой редакции чистил
    только сообщение; сегодня в `telegram_client.py` цепочка исключений
    обнулена намеренно, и утечки через `exc_info` быть не может, но
    будущий `raise ... from error` вернул бы её молча, без единого
    красного теста, — поэтому вторая половина закрыта здесь и сейчас, а не
    когда это станет фактом.
    """

    def __init__(self, secret: str, replacement: str) -> None:
        super().__init__()
        self.secret = secret
        self.replacement = replacement

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.secret in message:
            # Аргументы уже подставлены, поэтому их надо снять: иначе
            # обработчик подставил бы их второй раз в уже готовый текст.
            record.msg = message.replace(self.secret, self.replacement)
            record.args = None
        if record.exc_info:
            text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            if self.secret in text:
                record.exc_text = text.replace(self.secret, self.replacement)
            elif record.exc_text is None:
                # Досчитано один раз здесь — пусть `Formatter.format()` не
                # считает то же самое ещё раз.
                record.exc_text = text
        return True


def redact_secret(secret: str, replacement: str) -> None:
    """Зарегистрировать секрет и вычистить его из ТЕКУЩИХ обработчиков корня.

    Регистрация не зависит от момента вызова относительно `setup_logging`
    (спека приёмника telegram §4, находка item 6): применяется сразу к
    тому, что есть, а любой следующий `setup_logging` подхватит весь
    список для обработчиков, которые создаст он сам.
    """
    if (secret, replacement) not in _secrets:
        _secrets.append((secret, replacement))
    _apply_secrets(logging.getLogger().handlers)


def _apply_secrets(handlers: list[logging.Handler]) -> None:
    for secret, replacement in _secrets:
        for handler in handlers:
            if not any(
                isinstance(existing, TokenFilter) and existing.secret == secret
                for existing in handler.filters
            ):
                handler.addFilter(TokenFilter(secret, replacement))


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
    _apply_secrets(handlers)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        root.error("логи пишутся только в stdout: каталог %s недоступен (%s)", logs_dir, file_error)
