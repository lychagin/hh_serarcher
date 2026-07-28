"""Замок прогона: свойства, ради которых он именно `flock`."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hh_search.runlock import RunInProgress, single_run


def test_the_lock_is_exclusive(tmp_path: Path) -> None:
    """Второй захватить не может: `flock` привязан к описанию открытого файла,
    поэтому конфликт есть даже внутри одного процесса."""
    lock = tmp_path / "hh.db.lock"
    with single_run(lock), pytest.raises(RunInProgress, match="уже идёт"):
        with single_run(lock):
            pass  # pragma: no cover - сюда управление не приходит


def test_the_lock_is_free_again_after_the_block(tmp_path: Path) -> None:
    lock = tmp_path / "hh.db.lock"
    with single_run(lock):
        pass
    with single_run(lock):
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_an_exception_inside_the_block_releases_the_lock(tmp_path: Path) -> None:
    """Иначе первая же ошибка прогона запирала бы сервис до перезапуска."""
    lock = tmp_path / "hh.db.lock"
    with pytest.raises(ZeroDivisionError), single_run(lock):
        raise ZeroDivisionError
    with single_run(lock):
        pass


def test_a_killed_holder_does_not_leave_the_lock_taken(tmp_path: Path) -> None:
    """Главная причина выбрать `flock`, а не файл-часовой.

    SIGKILL и OOM-kill не дают процессу ничего убрать за собой, и замок
    на `O_CREAT|O_EXCL` после первого же убитого контейнера требовал бы
    ручной уборки — то есть чинил бы редкую беду ценой частой. `flock`
    снимает ядро вместе с процессом.
    """
    lock = tmp_path / "hh.db.lock"
    ready = tmp_path / "ready"
    script = tmp_path / "holder.py"
    script.write_text(
        textwrap.dedent(f"""
            import sys, time, pathlib
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from hh_search.runlock import single_run
            with single_run(pathlib.Path({str(lock)!r})):
                pathlib.Path({str(ready)!r}).write_text("ok")
                time.sleep(60)
        """),
        encoding="utf-8",
    )
    holder = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "держатель замка не стартовал"
        with pytest.raises(RunInProgress):
            with single_run(lock):
                pass  # pragma: no cover - сюда управление не приходит
        holder.kill()
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:  # pragma: no cover - только при аварии теста
            holder.kill()
            holder.wait(timeout=10)

    with single_run(lock):
        pass
