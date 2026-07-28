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

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hh_search.logging_setup import setup_logging


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
