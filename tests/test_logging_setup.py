"""Логирование — единственный канал, по которому виден отказ.

Модуль был покрыт только фактом своего существования: четыре независимые
мутации в нём выживали весь набор тестов. Каждая из них — тихая потеря
наблюдаемости, а не косметика:

* убрать обработчик stdout — `docker logs` пустеет, и на VPS не остаётся
  ничего, кроме файла, который ещё надо найти;
* не глушить `httpx` — INFO на каждый запрос (включая robots.txt) топит
  наши `ERROR`, а именно они здесь единственный способ узнать о потере
  данных;
* не чистить прежние обработчики — каждая команда добавляет свои, и
  записи двоятся, троятся и так далее;
* заглушить жалобу на недоступный каталог — логи молча пишутся только в
  stdout, и человек об этом не узнаёт.
"""

import io
import logging
import stat
import sys
from collections.abc import Iterator
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from hh_search.logging_setup import ResilientFileHandler, setup_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """`setup_logging` перенастраивает КОРНЕВОЙ логгер — вернём его на место."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, value in quiet.items():
        logging.getLogger(name).setLevel(value)


def test_logs_go_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Без обработчика stdout `docker logs` пуст, и на VPS не видно ничего."""
    setup_logging(tmp_path / "logs")
    logging.getLogger("hh_search.test").error("вакансия потеряна")
    assert "вакансия потеряна" in capsys.readouterr().out


def test_logs_go_to_the_rotating_file(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    setup_logging(logs)
    logging.getLogger("hh_search.test").error("вакансия потеряна")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "вакансия потеряна" in (logs / "hh.log").read_text(encoding="utf-8")


def test_httpx_chatter_is_silenced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """httpx на INFO пишет строку на КАЖДЫЙ запрос, включая robots.txt.

    За сутки это сотни строк, среди которых теряются наши `ERROR` — то
    есть глушение здесь не про аккуратность вывода, а про то, увидит ли
    человек сообщение о потере данных.
    """
    setup_logging(tmp_path / "logs")
    logging.getLogger("httpx").info("HTTP Request: GET https://hh.ru/robots.txt")
    logging.getLogger("httpcore").info("connect_tcp.started")
    logging.getLogger("hh_search.test").error("вакансия потеряна")
    captured = capsys.readouterr().out
    assert "robots.txt" not in captured and "connect_tcp" not in captured
    assert "вакансия потеряна" in captured


def test_a_second_setup_does_not_double_the_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Прежние обработчики снимаются, иначе записи двоятся.

    В одном процессе `setup_logging` вызывается КАЖДОЙ командой CLI, а в
    тестах — десятками раз подряд; без очистки к концу дня одна строка
    отчёта о потере превращается в десять одинаковых.
    """
    logs = tmp_path / "logs"
    setup_logging(logs)
    setup_logging(logs)
    logging.getLogger("hh_search.test").error("вакансия потеряна")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert capsys.readouterr().out.count("вакансия потеряна") == 1
    assert (logs / "hh.log").read_text(encoding="utf-8").count("вакансия потеряна") == 1


def test_an_unusable_log_directory_is_reported_and_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Недоступный каталог логов — не причина не искать вакансии.

    stdout остаётся, и в него же уходит жалоба на потерю файла: молчание
    здесь означало бы, что половина канала наблюдаемости отвалилась, а
    узнать об этом можно только заглянув в том.
    """
    occupied = tmp_path / "logs"
    occupied.write_text("не каталог", encoding="utf-8")

    setup_logging(occupied)

    logging.getLogger("hh_search.test").error("вакансия потеряна")
    captured = capsys.readouterr().out
    assert "только в stdout" in captured
    assert "вакансия потеряна" in captured
    assert any(
        getattr(handler, "stream", None) is sys.stdout for handler in logging.getLogger().handlers
    )


# --- каталог логов, ставший недоступным НА ХОДУ ---------------------------


def _break_the_log_directory(logs: Path) -> None:
    """Ротация на следующей же записи — и писать её некуда.

    Запись «до аварии» здесь обязательна, а не для красоты: начиная с
    3.12.4 CPython не ротирует ПУСТОЙ файл (`shouldRollover` возвращает
    False при нулевой позиции). На 3.12.3 ротация наступала и у пустого,
    поэтому тест, ломавший каталог до первой записи, был зелёным локально
    и красным в CI — расхождение поймал именно CI, на 3.12.13.
    Непустой файл ротируется на любой патч-версии, так что премисса
    «следующая запись пойдёт в doRollover» держится везде.
    """
    logging.getLogger("hh_search.test").info("до аварии")
    for handler in logging.getLogger().handlers:
        if isinstance(handler, ResilientFileHandler):
            handler.maxBytes = 1
    logs.chmod(stat.S_IRUSR | stat.S_IXUSR)


def test_a_log_directory_that_breaks_later_disables_the_file_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`setup_logging` проверяет каталог один раз — при старте.

    Дальше `doRollover` бросает `PermissionError` на КАЖДОЙ записи,
    `logging` исключение подавляет и печатает полный traceback в stderr:
    замер на FIX_BASE — 2855 байт мусора на пять записей (571 байт на
    запись), все записи в файл потеряны, процесс жив и выглядит здоровым.
    В докере stderr уходит в `docker logs`, то есть ротация лечится
    обратной стороной той же беды.

    Проверяется ровно это: мусора в stderr нет вовсе, жалоба одна, и
    сервис продолжает писать в stdout — то есть наблюдаемость не пропадает
    вместе с файлом.
    """
    logs = tmp_path / "logs"
    setup_logging(logs)
    _break_the_log_directory(logs)

    noise = io.StringIO()
    with redirect_stderr(noise):
        for number in range(5):
            logging.getLogger("hh_search.test").info("после аварии %d", number)
    logs.chmod(0o755)

    assert noise.getvalue() == ""
    captured = capsys.readouterr().out
    assert captured.count("файловый лог") == 1
    assert "отключён после первой же ошибки записи" in captured
    # Записи, шедшие после отказа, никуда не делись: stdout остался.
    assert captured.count("после аварии") == 5


def test_the_broken_file_handler_stops_trying(tmp_path: Path) -> None:
    """Отключён — значит отключён: обработчик больше не трогает файл.

    Без флага каждая следующая запись снова шла бы в `doRollover`, снова
    получала `PermissionError` и снова стоила бы полного traceback: беда
    здесь не в одной потерянной записи, а в том, что цена платится
    вечно и растёт вместе с трафиком лога.
    """
    logs = tmp_path / "logs"
    setup_logging(logs)
    handler = next(
        item for item in logging.getLogger().handlers if isinstance(item, ResilientFileHandler)
    )
    _break_the_log_directory(logs)
    with redirect_stderr(io.StringIO()):
        logging.getLogger("hh_search.test").info("первая после аварии")
    logs.chmod(0o755)

    assert handler.disabled_by is not None
    assert "Permission denied" in handler.disabled_by
    # Дальнейшие записи в файл не идут вовсе — даже когда права вернулись:
    # причина отказа записи сама не проходит, а проверять её на каждой
    # строке значит платить за неё на каждой строке.
    logging.getLogger("hh_search.test").info("после возврата прав")
    assert "после возврата прав" not in (logs / "hh.log").read_text(encoding="utf-8")
