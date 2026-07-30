"""Приёмники отчётов и их фабрика.

`build_sinks` вызывается ДО `start_run()` и до первого сетевого запроса
(требование спеки §7/§9: опечатка роняет процесс на старте). Иначе
неизвестное имя в `app.yaml` обнаруживается в середине прогона — когда за
страницы уже заплачено запросами к hh.ru, а отчёт всё равно не выйдет.
Проверять имена типом (`Literal["csv", "markdown"]` в конфиге) не стали:
это раздвоило бы список приёмников между схемой конфига и этой фабрикой,
а расширение через `Sink` — заявленная точка роста (спека §4.2).
"""

import os
from collections.abc import Sequence
from pathlib import Path

from hh_search.sinks.base import Sink
from hh_search.sinks.csv_sink import CsvSink
from hh_search.sinks.markdown_sink import MarkdownSink
from hh_search.sinks.telegram_client import TelegramClient, TelegramCredentials
from hh_search.sinks.telegram_sink import TelegramSink

__all__ = ["CsvSink", "MarkdownSink", "Sink", "TelegramSink", "build_sinks"]


def build_sinks(names: Sequence[str], reports_dir: Path, threshold: float) -> list[Sink]:
    sinks: list[Sink] = []
    for name in names:
        if name == "csv":
            sinks.append(CsvSink(reports_dir))
        elif name == "markdown":
            sinks.append(MarkdownSink(reports_dir, threshold))
        elif name == "telegram":
            # Секреты читаются здесь, а не в конфиге: фабрика зовётся до
            # `start_run()` и до сети, поэтому недописанный `.env` роняет
            # процесс на старте — как и всякий дефект конфигурации (§4).
            credentials = TelegramCredentials.from_env(os.environ)
            sinks.append(TelegramSink(reports_dir, threshold, TelegramClient(credentials)))
        else:
            # По-русски и без префикса от вызывающего: текст уходит человеку
            # как есть, а «неизвестный sink» внутри «неизвестный приёмник:»
            # давало заикание (находка I4).
            raise ValueError(f"неизвестный приёмник: {name}")
    return sinks
