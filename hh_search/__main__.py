"""CLI (спека §8.3). Конфиг читается ЛЕНИВО, внутри команды.

`@app.callback()`, загружающий конфиг, ломал две вещи сразу: `--help` любой
подкоманды требовал существующего `/data/config`, а отсутствие конфига
давало голый traceback вместо внятного сообщения. Здесь callback запоминает
только каталог, а читает его та команда, которой конфиг действительно нужен.
"""

import contextlib
import errno
import logging
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.errors import AccessForbidden, StorageUnavailable
from hh_search.logging_setup import setup_logging
from hh_search.pipeline import EXIT_CODES, OK, PARTIAL, RunStats, run_once
from hh_search.pipeline.reporting import emit_to_sinks, maintain_sinks
from hh_search.runlock import RunInProgress, single_run
from hh_search.scheduler import EXIT_FORBIDDEN, StopSignal, serve
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks import build_sinks
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)
# `pretty_exceptions_enable=False` — это про читаемость логов сервиса, а не
# про вкус. Цветной трейсбек rich раскладывает одну ошибку на 20–50 строк с
# рамками и подсветкой; в журнале контейнера, который читают через
# `docker logs` и grep, это ровно та форма, в которой причину найти труднее
# всего. Зависимость от rich так не снимается (её тянет сам typer, ~13 МБ
# образа), и снять её нечем, кроме отказа от typer, — но 13 МБ образа стоят
# дешевле нечитаемого журнала, а вот обратный размен неверен.
app = typer.Typer(
    help="Автопоиск вакансий на hh.ru", no_args_is_help=True, pretty_exceptions_enable=False
)

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
# Файл-маркер устойчивого 403. `restart: unless-stopped` перезапускает
# контейнер на любом коде возврата, поэтому «демон остановлен» одним кодом
# в описанном спекой развёртывании недостижимо; маркер достигает того,
# ради чего остановка требуется, — ни одного запроса к hh.ru.
FORBIDDEN_MARKER = "access-forbidden.stop"
# Доля интервала, внутри которой повторный прогон считается лишним.
#
# Проверки «прошлый прогон был только что» не было нигде, а `docker
# restart` немедленно запускает полный обход листингов: замер — `up -d`
# плюс три `restart` за 48 секунд дали 41 запрос к hh.ru. При
# `restart: unless-stopped` и любой падающей конфигурации это множитель
# запросов, а при обычной возне владельца («поправил профиль —
# перезапустил») — десятки лишних обходов за вечер.
#
# Половина выбрана из потолка, который она может создать: пропущенный
# прогон не сдвигает дедлайн следующего, поэтому худший случай — пауза в
# полтора интервала (пропуск за миг до порога плюс полный интервал
# ожидания). Это строго меньше двух интервалов, на которых краснеет
# healthcheck, — то есть предохранитель не способен сам по себе погасить
# индикатор. Доля крупнее (0.9) такой гарантии уже не даёт.
MIN_RUN_INTERVAL_FRACTION = 0.5

ConfigDir = Annotated[Path | None, typer.Option("--config-dir", help="Каталог с YAML-конфигами")]
Since = Annotated[str, typer.Option("--since", help="Период в днях: 7 или 7d")]


@app.callback()
def main(ctx: typer.Context, config_dir: ConfigDir = None) -> None:
    """Запоминает каталог конфигов. Ничего не читает и не создаёт."""
    ctx.obj = config_dir or Path(os.environ.get("HH_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _die(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


# Ошибки, за которыми почти всегда стоит одно и то же: uid контейнера не
# совпал с владельцем тома, либо том смонтирован `:ro`. sqlite3 в эту пару
# входит не для красоты — свой отказ открытия файла он отдаёт
# `OperationalError` без errno, мимо иерархии OSError.
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
# Текст sqlite3 при отказе доступа к файлу базы. Сверяется по вхождению,
# потому что другого признака у `OperationalError` нет вовсе.
_SQLITE_DENIED = ("unable to open database file", "readonly database", "attempt to write")


def _looks_denied(error: Exception) -> bool:
    if isinstance(error, sqlite3.Error):
        return any(mark in str(error) for mark in _SQLITE_DENIED)
    return isinstance(error, OSError) and error.errno in _DENIED_ERRNOS


def _owner(path: Path) -> tuple[Path, int, int] | None:
    """Владелец ближайшего существующего каталога вверх по пути."""
    for candidate in (path, *path.parents):
        try:
            info = candidate.stat()
        except OSError:
            continue
        return candidate, info.st_uid, info.st_gid
    return None


def _advice(state_dir: Path) -> str:
    """Совет, отличающий чужой uid от своих же прав, — по факту, не наугад.

    Разница видна `stat`, и путать её нельзя: при совпадающем владельце
    правка `HH_UID` не лечит ничего, а человек уже поверил, что дело в ней.
    """
    holder = _owner(state_dir)
    if holder is None:
        return f"владельца {state_dir} определить не удалось: каталога нет ни на одном уровне"
    path, uid, gid = holder
    mine = f"процесс работает от uid={os.getuid()} gid={os.getgid()}"
    if uid == os.getuid():
        return (
            f"{mine}, и {path} принадлежит тому же uid — значит дело не в нём, а в правах "
            "самого каталога (chmod) либо в томе, смонтированном только на чтение (:ro)"
        )
    return (
        f"{mine}, а {path} принадлежит uid={uid} gid={gid}. Это и есть обычная причина: "
        "bind-mount подменяет каталог образа хостовым вместе с владельцем. Приведите "
        "HH_UID/HH_GID в .env к владельцу ./data — "
        'printf \'HH_UID=%s\\nHH_GID=%s\\n\' "$(id -u)" "$(id -g)" >> .env — '
        "и пересоздайте контейнер: docker compose up -d --force-recreate"
    )


def _storage_message(config: Config, error: Exception) -> str:
    """Внятная причина вместо трейсбека — самая вероятная ошибка первого дня.

    Замер на FIX_BASE: `init-db` от чужого uid давал 22 строки
    rich-трейсбека, `run` на созданной базе — 52, том `:ro` — столько же.
    Владелец при этом видел `OperationalError: unable to open database
    file`, то есть текст, из которого не следует ни причина, ни действие.
    Контраст с недоступным каталогом ОТЧЁТОВ (внятный текст, статус
    `failed`, вакансии сохранены) и делал эту ветку заметной.
    """
    state = config.app.paths.state
    where = f"{state.parent} (база {state.name})"
    if _looks_denied(error):
        return f"нет доступа к каталогу данных {where}: {error}. {_advice(state.parent)}"
    if isinstance(error, sqlite3.Error):
        # Не про права: файл есть, а прочитать его нечем. Чаще всего это
        # база без схемы — и совет тут другой, поэтому и текст другой.
        return f"база {state} не читается: {error}. Если схемы в ней нет — сначала `init-db`"
    return f"каталог данных {where} недоступен: {error}"


@contextlib.contextmanager
def _storage_errors(config: Config) -> Iterator[None]:
    """Превратить отказ тома в сообщение. Внутри — только работа с диском."""
    try:
        yield
    except (OSError, sqlite3.Error) as error:
        raise StorageUnavailable(_storage_message(config, error)) from error


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
        # Текст ошибки уходит как есть, без префикса: `build_sinks`
        # отказывает и по неизвестному имени, и по незаданным секретам
        # приёмника, а прежний префикс называл единственную причину —
        # «в app.yaml неизвестный приёмник» — и на второй из них врал
        # дважды. Приёмник известен, `app.yaml` ни при чём, переменные
        # читаются из окружения; человек шёл править не тот файл.
        _die(str(error), EXIT_CONFIG)


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


def _forbidden_marker(config: Config) -> Path:
    return config.app.paths.state.parent / FORBIDDEN_MARKER


def _refuse_while_forbidden(config: Config) -> None:
    """Пока стоит маркер, в сеть не идёт НИ ОДНА команда, а не только `serve`.

    Контракт маркера дословно: «перезапущенный контейнер не отправляет к
    hh.ru ни одного запроса». Проверка жила в `serve_command`, поэтому
    предохранитель открывался самым естественным действием человека:
    увидев остановленный демон, он выполняет `run` («а сейчас как?») —
    и получает полный прогон (замер на FIX_BASE: 11 запросов). Отсюда
    общая для обеих сетевых команд проверка: `run` и `serve` — это две
    двери в одну и ту же сеть.
    """
    marker = _forbidden_marker(config)
    if marker.exists():
        _die(
            f"доступ к hh.ru был закрыт устойчиво, стоит маркер {marker}. "
            "В сеть не уходит ни одного запроса. Разберитесь и удалите файл, "
            "если считаете, что можно продолжать",
            EXIT_FAILED,
        )


def _too_soon(config: Config, repo: SqliteRepository) -> str | None:
    """Причина пропустить прогон, если предыдущий успешный был только что.

    Смотрит на `last_successful_run()` — тот же источник правды, что и у
    healthcheck. Отказавший прогон не считается: повторить его сразу
    после починки конфига — как раз то, чего человек и хочет.
    """
    last = repo.last_successful_run()
    if last is None:
        return None
    cooldown = timedelta(hours=config.app.schedule.interval_hours * MIN_RUN_INTERVAL_FRACTION)
    age = datetime.now(UTC) - last
    if age >= cooldown:
        return None
    return (
        f"прошлый успешный прогон закончился {last.isoformat()} "
        f"({age.total_seconds() / 60:.0f} мин назад), а интервал — "
        f"{config.app.schedule.interval_hours} ч. Прогон пропущен, "
        f"следующий возможен через {(cooldown - age).total_seconds() / 60:.0f} мин"
    )


def _lock_path(config: Config) -> Path:
    state = config.app.paths.state
    return state.with_name(state.name + ".lock")


def _execute(config: Config, sinks: Sequence[Sink]) -> RunStats | None:
    """Один прогон. `None` — прогон пропущен, потому что предыдущий был только что.

    Проверка стоит ВНУТРИ замка и до создания клиента: решение принимает
    тот, кто действительно сейчас работает, и ни одного соединения при
    отказе не открывается.
    """
    # Каталог создаётся здесь, а не только в `init-db`: на пустом volume
    # `sqlite3.connect` падает «unable to open database file» ещё до
    # `init_schema()`, и первый прогон давал голый traceback, требуя
    # необъявленного порядка команд.
    # Весь шаг под охраной: отказ тома одинаково вероятен на mkdir, на
    # замке, на открытии базы и на первой же записи в неё, а причина у
    # всех четырёх одна и лечится одним действием.
    with _storage_errors(config):
        config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
        with single_run(_lock_path(config)), SqliteRepository(config.app.paths.state) as repo:
            repo.init_schema()
            skip = _too_soon(config, repo)
            if skip is not None:
                logger.info("%s", skip)
                return None
            # Ровно здесь и нигде раньше: замок уже наш, значит любая строка
            # `running` осталась от умершего процесса, а не от живого соседа.
            repo.close_abandoned_runs()
            with PoliteClient(config.app.http, config.app.user_agent) as client:
                return run_once(config, client, repo, KeywordScorer(config.profile), sinks)


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Создать схему базы (и догнать существующую до неё)."""
    config = _config(ctx)
    try:
        with _storage_errors(config):
            config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
            with SqliteRepository(config.app.paths.state) as repo:
                repo.init_schema()
    except StorageUnavailable as error:
        _die(str(error), EXIT_FAILED)
    typer.echo(f"схема создана: {config.app.paths.state}")


@app.command("run")
def run_command(ctx: typer.Context) -> None:
    """Выполнить один прогон. Код возврата повторяет статус прогона."""
    config = _config(ctx)
    sinks = _sinks(config)
    _refuse_while_forbidden(config)
    try:
        stats = _execute(config, sinks)
    except RunInProgress as error:
        _die(f"{error}. Дождитесь его конца или остановите `serve`", EXIT_FAILED)
    except StorageUnavailable as error:
        _die(str(error), EXIT_FAILED)
    except AccessForbidden as error:
        # Текст исключения уже кончается словами «Прогон остановлен,
        # обходные пути не применяются» — повторять их здесь значило
        # печатать одно и то же дважды подряд в одной строке.
        _die(f"hh.ru закрыл доступ: {error}", EXIT_FAILED)
    if stats is None:
        # Не ошибка и не молчаливый ноль: человек, запустивший `run`
        # руками, обязан узнать, ПОЧЕМУ отчёта не будет и когда будет.
        typer.echo("прогон пропущен: предыдущий успешный был слишком недавно (см. лог)")
        raise typer.Exit(EXIT_CODES[OK])
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
    _refuse_while_forbidden(config)
    marker = _forbidden_marker(config)
    stop = StopSignal()
    stop.install()
    logger.info("старт, интервал %d ч", config.app.schedule.interval_hours)
    code = serve(config, lambda: _execute(config, sinks), stop=stop)
    if code == EXIT_FORBIDDEN:
        # `restart: unless-stopped` перезапускает контейнер на ЛЮБОМ коде
        # возврата, включая нулевой, — то есть заявленная спекой §9
        # остановка одним кодом в описанном развёртывании недостижима.
        # Маркер достигает того, ради чего остановка и требуется:
        # перезапущенный контейнер увидит файл и не отправит к hh.ru ни
        # одного запроса. Docker разводит рестарты экспоненциально, а
        # healthcheck при этом красный.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"{datetime.now(UTC).isoformat()} доступ закрыт устойчиво; "
            "удалите этот файл, когда разберётесь\n",
            encoding="utf-8",
        )
        logger.error("поставлен маркер %s: до его удаления демон в сеть не пойдёт", marker)
    raise typer.Exit(code)


@app.command("healthcheck")
def healthcheck(ctx: typer.Context) -> None:
    """Код 0, если последний успешный прогон свежее двух интервалов."""
    config = _config(ctx)
    deadline = datetime.now(UTC) - timedelta(hours=2 * config.app.schedule.interval_hours)
    with _open(config) as repo:
        try:
            last = repo.last_successful_run()
            # Счётчики прогона пишутся с самого начала, и до сих пор их не
            # читал никто. Читаются они здесь: команда, которой человек
            # проверяет сервис, обязана отвечать не только «жив/не жив», но
            # и «что именно делал последний прогон» — иначе за ответом
            # приходится лезть в sqlite3 руками.
            latest = repo.last_run()
        except sqlite3.Error as error:
            # Тип исключения — не SQL: файл базы есть, но схемы в нём нет
            # (или он не база вовсе). Для healthcheck это «работа не
            # делается», а не повод падать traceback'ом.
            _die(f"журнал прогонов не читается: {error}. Сначала `init-db`", EXIT_FAILED)
    summary = f"; {latest.describe()}" if latest else ""
    if last is None or last < deadline:
        typer.echo(
            f"последний успешный прогон: {last.isoformat() if last else 'никогда'}, "
            f"порог: {deadline.isoformat()}{summary}",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    typer.echo(f"ok, последний успешный прогон: {last.isoformat()}{summary}")


@app.command("mark")
def mark(ctx: typer.Context, vacancy_id: str, status: str) -> None:
    """Проставить статус вакансии вручную."""
    config = _config(ctx)
    if status not in MANUAL_STATUSES:
        _die(f"неизвестный статус {status!r}; допустимы: {', '.join(MANUAL_STATUSES)}", EXIT_CONFIG)
    try:
        with _storage_errors(config), _open(config) as repo:
            changed = repo.set_status(vacancy_id, status)
    except StorageUnavailable as error:
        _die(str(error), EXIT_FAILED)
    if not changed:
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
    limit = config.app.limits.rows_per_batch
    try:
        with _storage_errors(config), _open(config) as repo:
            vacancies = repo.reported_since(cutoff, limit)
    except StorageUnavailable as error:
        _die(str(error), EXIT_FAILED)
    if not vacancies:
        typer.echo(f"с {cutoff:%Y-%m-%d} отправленных вакансий не найдено")
        return
    if len(vacancies) == limit:
        # Усечение обязано быть сказано вслух: здесь его последствия видит
        # человек, а не следующий прогон. `report --since 60` на базе с
        # 22 000 вакансий стоил 351 МБ RSS, то есть OOM по команде
        # человека на VPS с 512 МБ; потолок это лечит, но неполный отчёт,
        # выданный за полный, — это ровно та тихая потеря, ради которой
        # потолок и вводился. Отбор идёт по убыванию оценки, поэтому
        # усечён всегда хвост.
        typer.echo(
            f"отчёт усечён потолком app.limits.rows_per_batch = {limit}: взяты "
            f"{limit} вакансий с самой высокой оценкой, за границей могли остаться "
            "другие. Сузьте --since или поднимите потолок",
            err=True,
        )
    # Тот же замок, что у прогона: `report` пишет в ТОТ ЖЕ файл дня, а
    # дедупликация приёмника — чтение-правка-запись, и гонится она с
    # прогоном ровно так же, как два прогона гонятся между собой.
    try:
        with single_run(_lock_path(config)):
            # `maintain_sinks` — тем же порядком, что и в `report()` из
            # конвейера (`pipeline/reporting.py`): без неё `report --since`
            # перестал бы и убирать черновики telegram, и довозить
            # застрявшие документы, хотя раньше делал и то и другое через
            # `emit`. Докстринг `emit_to_sinks` называет это явно: `report`
            # в CLI обязан вести себя ровно так же, как `run`.
            #
            # Через тот же `emit_to_sinks`, что и конвейер: недоступный
            # каталог отчётов давал здесь голый traceback, тогда как `run`
            # в этой же ситуации отдаёт внятный текст и ненулевой код. По
            # выводу `report` человек решает, чинить ему конфиг или том, —
            # двух разных ответов на один отказ у CLI быть не должно.
            maintain_sinks(sinks, datetime.now(UTC))
            written, failed = emit_to_sinks(sinks, vacancies, datetime.now(UTC))
    except RunInProgress as error:
        _die(f"{error}. Отчёт пишется в тот же файл дня, поэтому ждём", EXIT_FAILED)
    if failed:
        typer.echo(
            f"приёмники не приняли отчёт: {', '.join(failed)}; "
            f"приняли: {', '.join(written) or 'ни один'}",
            err=True,
        )
        raise typer.Exit(EXIT_CODES[PARTIAL])
    # Сколько ЗАПИСАНО, а не сколько отдано: приёмники пропускают то, что
    # уже стоит в отчёте дня, и «перегенерировано 143» при нуле новых
    # строк отправляло человека искать несуществующие изменения.
    report_line = ", ".join(f"{name}: {count}" for name, count in written.items())
    typer.echo(
        f"отдано приёмникам вакансий: {len(vacancies)}; записано новых строк — {report_line}"
    )


if __name__ == "__main__":
    app()
