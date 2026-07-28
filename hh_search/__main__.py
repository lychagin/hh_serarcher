"""CLI (спека §8.3). Конфиг читается ЛЕНИВО, внутри команды.

`@app.callback()`, загружающий конфиг, ломал две вещи сразу: `--help` любой
подкоманды требовал существующего `/data/config`, а отсутствие конфига
давало голый traceback вместо внятного сообщения. Здесь callback запоминает
только каталог, а читает его та команда, которой конфиг действительно нужен.
"""

import logging
import os
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.logging_setup import setup_logging
from hh_search.pipeline import OK, RunStats, run_once
from hh_search.scheduler import StopSignal, serve
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks import build_sinks
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)
app = typer.Typer(help="Автопоиск вакансий на hh.ru", no_args_is_help=True)

DEFAULT_CONFIG_DIR = Path("/data/config")
# 2 — код click'а для ошибки в аргументах; ошибка конфига по смыслу та же.
# 1 и 3 приходят из статуса прогона (см. pipeline/stats.py).
EXIT_CONFIG = 2
EXIT_FAILED = 1
# Ручные статусы из спеки §5.2 плюс те, что ставит конвейер: `mark`
# получает статус от человека, а `set_status` его не валидирует, и опечатка
# (`mark 1 aplied`) увела бы вакансию в состояние, невидимое всем выборкам.
MANUAL_STATUSES = ("interesting", "applied", "archived", "new", "rejected", "reported")
_SINCE_RE = re.compile(r"^(\d+)\s*d?$")

ConfigDir = Annotated[Path | None, typer.Option("--config-dir", help="Каталог с YAML-конфигами")]
Since = Annotated[str, typer.Option("--since", help="Период в днях: 7 или 7d")]


@app.callback()
def main(ctx: typer.Context, config_dir: ConfigDir = None) -> None:
    """Запоминает каталог конфигов. Ничего не читает и не создаёт."""
    ctx.obj = config_dir or Path(os.environ.get("HH_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _die(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _config(ctx: typer.Context) -> Config:
    """Прочитать конфиг и включить логи. Ошибка конфига — внятный текст."""
    config_dir = ctx.obj if isinstance(ctx.obj, Path) else DEFAULT_CONFIG_DIR
    try:
        config = load_config(config_dir)
    except (OSError, ValueError) as error:
        _die(f"конфиг в {config_dir} не прочитан: {error}", EXIT_CONFIG)
    setup_logging(config.app.paths.logs)
    return config


def _sinks(config: Config) -> list[Sink]:
    """Приёмники строятся ДО сети и до `start_run()` — контракт задачи 9.

    В режиме `serve` это ещё важнее: собранное внутри прогона неизвестное
    имя приёмника попало бы в `except Exception` планировщика, и демон
    крутил бы бесполезный цикл каждые четыре часа.
    """
    try:
        return build_sinks(
            config.app.sinks, config.app.paths.reports, config.profile.report_threshold
        )
    except ValueError as error:
        _die(f"в app.yaml неизвестный приёмник: {error}", EXIT_CONFIG)


def _open(config: Config) -> SqliteRepository:
    """Открыть существующую базу. Отсутствие файла — не повод создавать его.

    `sqlite3.connect` создаёт файл молча, поэтому `healthcheck` до
    `init-db` не только падал `OperationalError`, но и оставлял после себя
    нулевой файл базы. Docker дёргает HEALTHCHECK с первых секунд жизни
    контейнера — то есть ровно в этот момент.
    """
    if not config.app.paths.state.exists():
        _die(f"базы нет: {config.app.paths.state}. Сначала `init-db`", EXIT_FAILED)
    return SqliteRepository(config.app.paths.state)


def _execute(config: Config, sinks: Sequence[Sink]) -> RunStats:
    # Каталог создаётся здесь, а не только в `init-db`: на пустом volume
    # `sqlite3.connect` падает «unable to open database file» ещё до
    # `init_schema()`, и первый прогон давал голый traceback, требуя
    # необъявленного порядка команд.
    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    with (
        SqliteRepository(config.app.paths.state) as repo,
        PoliteClient(config.app.http, config.app.user_agent) as client,
    ):
        repo.init_schema()
        return run_once(config, client, repo, KeywordScorer(config.profile), sinks)


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Создать схему базы (и догнать существующую до неё)."""
    config = _config(ctx)
    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    with SqliteRepository(config.app.paths.state) as repo:
        repo.init_schema()
    typer.echo(f"схема создана: {config.app.paths.state}")


@app.command("run")
def run_command(ctx: typer.Context) -> None:
    """Выполнить один прогон. Код возврата повторяет статус прогона."""
    config = _config(ctx)
    sinks = _sinks(config)
    try:
        stats = _execute(config, sinks)
    except AccessForbidden as error:
        _die(f"hh.ru закрыл доступ: {error}. Обходные пути не применяются", EXIT_FAILED)
    if stats.status != OK:
        # Молчаливый ноль здесь — это cron, который никогда не узнает, что
        # отчёт не вышел: ровно тот случай, ради которого статус прогона и
        # существует.
        typer.echo(f"прогон завершён со статусом {stats.status}: {stats.error}", err=True)
    raise typer.Exit(stats.exit_code())


@app.command("serve")
def serve_command(ctx: typer.Context) -> None:
    """Бесконечный цикл прогонов по расписанию (точка входа контейнера)."""
    config = _config(ctx)
    sinks = _sinks(config)
    stop = StopSignal()
    stop.install()
    logger.info("старт, интервал %d ч", config.app.schedule.interval_hours)
    raise typer.Exit(serve(config, lambda: _execute(config, sinks), stop=stop))


@app.command("healthcheck")
def healthcheck(ctx: typer.Context) -> None:
    """Код 0, если последний успешный прогон свежее двух интервалов."""
    config = _config(ctx)
    deadline = datetime.now(UTC) - timedelta(hours=2 * config.app.schedule.interval_hours)
    with _open(config) as repo:
        try:
            last = repo.last_successful_run()
        except sqlite3.Error as error:
            # Тип исключения — не SQL: файл базы есть, но схемы в нём нет
            # (или он не база вовсе). Для healthcheck это «работа не
            # делается», а не повод падать traceback'ом.
            _die(f"журнал прогонов не читается: {error}. Сначала `init-db`", EXIT_FAILED)
    if last is None or last < deadline:
        typer.echo(
            f"последний успешный прогон: {last.isoformat() if last else 'никогда'}, "
            f"порог: {deadline.isoformat()}",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    typer.echo(f"ok, последний успешный прогон: {last.isoformat()}")


@app.command("mark")
def mark(ctx: typer.Context, vacancy_id: str, status: str) -> None:
    """Проставить статус вакансии вручную."""
    config = _config(ctx)
    if status not in MANUAL_STATUSES:
        _die(f"неизвестный статус {status!r}; допустимы: {', '.join(MANUAL_STATUSES)}", EXIT_CONFIG)
    with _open(config) as repo:
        if not repo.set_status(vacancy_id, status):
            _die(f"вакансии {vacancy_id} нет в базе, статус не изменён", EXIT_FAILED)
    typer.echo(f"{vacancy_id} → {status}")


@app.command("report")
def report_command(ctx: typer.Context, since: Since = "7d") -> None:
    """Перегенерировать отчёт из базы по уже отправленным вакансиям."""
    config = _config(ctx)
    match = _SINCE_RE.match(since.strip())
    if match is None:
        _die(f"--since ожидает число дней (7 или 7d), получено {since!r}", EXIT_CONFIG)
    sinks = _sinks(config)
    cutoff = datetime.now(UTC) - timedelta(days=int(match.group(1)))
    with _open(config) as repo:
        vacancies = repo.reported_since(cutoff)
    if not vacancies:
        typer.echo(f"с {cutoff:%Y-%m-%d} отправленных вакансий не найдено")
        return
    for sink in sinks:
        sink.emit(vacancies, datetime.now(UTC))
    typer.echo(f"перегенерировано вакансий: {len(vacancies)}")


if __name__ == "__main__":
    app()
