"""Взаимное исключение прогонов: в один момент времени работает ровно один.

Взаимного исключения не было нигде — ни в CLI, ни в хранилище, — а
`docker exec … run` во время работы `serve` это самое обычное действие
человека, которому не хочется ждать четыре часа. Данные в базе при этом
целы (каждая запись атомарна), но отчёт задваивается: дедупликация
приёмника — read-modify-write по файлу дня, и она гонится. Замер на
четырёх параллельных `run`: 38 строк вместо 19, 18 дублей и второй BOM
в СЕРЕДИНЕ файла, хотя приёмник обещает ровно один BOM за файл.

Замок именно файловый (`flock`), а не строка в SQLite и не
`BEGIN IMMEDIATE`. Причин две.

1. Прогон коммитит десятки раз, поэтому транзакция, которая держалась бы
   всё это время, невозможна: удерживаемый второй связью RESERVED-замок
   блокировал бы записи самого прогона.
2. `flock` снимает ядро при смерти процесса — любой, включая SIGKILL и
   OOM-kill. Замок на `O_CREAT|O_EXCL` этим свойством не обладает и
   после первого же убитого контейнера требовал бы ручной уборки, то
   есть чинил бы одну редкую беду ценой другой, более частой.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from hh_search.errors import HhSearchError


class RunInProgress(HhSearchError):
    """Прогон уже идёт: замок держит другой процесс."""


@contextmanager
def single_run(path: Path) -> Iterator[None]:
    """Держать замок прогона на время блока. Занят — `RunInProgress`.

    Отказ немедленный (`LOCK_NB`), а не ожидание: человек, набравший
    `run` вручную, обязан увидеть «прогон уже идёт» сразу, а не зависнуть
    на неизвестный срок. Ждать нечего и по существу — очередь всё равно
    будет разобрана идущим прогоном.

    В файле остаётся pid держателя: он ничего не решает (решает сам
    `flock`), но отвечает на первый же вопрос — «а кто его держит».
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+", а не "w": открытие не должно затирать pid, записанный тем, кто
    # замком уже владеет. Усечение — только после успешного захвата.
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RunInProgress(
                f"прогон уже идёт: замок {path} занят другим процессом ({_holder(handle)})"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        # Закрытие дескриптора снимает flock; отдельный LOCK_UN не нужен и
        # был бы лишним окном между снятием замка и закрытием файла.
        handle.close()


def _holder(handle: TextIO) -> str:
    """Кто держит замок — по содержимому файла. Только для сообщения."""
    handle.seek(0)
    content = handle.read().strip()
    return f"pid {content}" if content else "pid неизвестен"
