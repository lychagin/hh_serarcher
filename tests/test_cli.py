import fcntl
import logging
import os
import signal
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from hh_search.__main__ import app
from hh_search.config.loader import load_config
from hh_search.scheduler import StopSignal
from hh_search.storage.repository import SqliteRepository
from tests.test_config import APP_YAML, write_config
from tests.test_pipeline import TWO_VACANCIES, page_html

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://hh.ru/vacancies/programmist"
PAGE_PATTERN = r"^https://hh\.ru/vacancy/\d+$"
TODAY = f"{datetime.now(UTC):%Y-%m-%d}"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`setup_logging` перенастраивает КОРНЕВОЙ логгер — вернём его на место.

    Иначе файловый обработчик, открытый на удалённый `tmp_path`, остаётся
    висеть на весь остаток прогона тестов.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def prepare(tmp_path: Path, **overrides: str) -> Path:
    """Каталог конфигов, у которого все пути ведут в `tmp_path`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    app_yaml = (
        # Пауза между запросами — минимальная разрешённая: тесты ходят через
        # настоящий `time.sleep` (клиент собирает сам CLI), и секунда на
        # запрос превращала весь файл в двадцать три секунды.
        APP_YAML.replace("delay_between_requests_sec: 1.0", "delay_between_requests_sec: 0.1")
        .replace("/data/state", str(tmp_path / "state"))
        .replace("/data/reports", str(tmp_path / "reports"))
        .replace("/data/logs", str(tmp_path / "logs"))
    )
    write_config(config_dir, **{"app.yaml": app_yaml, **overrides})
    return config_dir


def invoke(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["--config-dir", str(config_dir), *args])


def mock_source(listing_status: int = 200) -> None:
    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(listing_status, text=TWO_VACANCIES)
    )
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))


def state_path(config_dir: Path) -> Path:
    return load_config(config_dir).app.paths.state


# --- init-db и healthcheck: первые секунды жизни контейнера ----------------


def test_init_db_creates_the_state_file(tmp_path: Path) -> None:
    result = invoke(prepare(tmp_path), "init-db")
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hh.db").exists()


def test_healthcheck_before_init_db_fails_and_creates_nothing(tmp_path: Path) -> None:
    """Docker дёргает HEALTHCHECK с первых секунд, до первого `init-db`.

    Прежняя редакция падала `OperationalError` и оставляла после себя
    нулевой файл базы: `sqlite3.connect` создаёт файл молча, и следующий
    `init-db` работал уже по мусору.
    """
    config_dir = prepare(tmp_path)
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "init-db" in result.output
    assert not (tmp_path / "state" / "hh.db").exists()


def test_healthcheck_fails_on_a_database_without_schema(tmp_path: Path) -> None:
    """Файл есть, схемы нет — для healthcheck это «работа не делается».

    Кода возврата тут недостаточно: необработанное исключение внутри
    CliRunner тоже даёт единицу, поэтому проверяется ещё и сообщение —
    иначе тест зелен и на голом `OperationalError`.
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "hh.db").write_bytes(b"")
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "журнал прогонов не читается" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_healthcheck_passes_after_a_fresh_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "ok")
    assert invoke(config_dir, "healthcheck").exit_code == 0


def test_healthcheck_counts_a_partial_run_as_success(tmp_path: Path) -> None:
    """`partial` — успех для healthcheck: прогон состоялся, часть работы
    потеряна. Именно поэтому «прогон не сделал ничего» обязан быть
    `failed`, иначе индикатор зелен при полной тишине."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "partial")
    assert invoke(config_dir, "healthcheck").exit_code == 0


def test_healthcheck_fails_on_a_stale_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    stale = datetime.now(UTC) - timedelta(hours=24)
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "ok", finished_at=stale)
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "последний успешный прогон" in result.output


def test_failed_run_does_not_make_healthcheck_green(tmp_path: Path) -> None:
    """Строка журнала есть, но статус `failed` — индикатор обязан краснеть."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "failed", error="источник закрыт")
    assert invoke(config_dir, "healthcheck").exit_code == 1


def test_healthcheck_shows_what_the_last_run_did(tmp_path: Path) -> None:
    """Счётчики прогона писались всегда, а читать их было некому.

    Единственная выборка из `run` брала `finished_at`, поэтому человек,
    увидевший красный индикатор, узнавал только «свежего успешного
    прогона нет». Причина при этом лежит в той же строке журнала —
    достаточно её прочитать.
    """
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(
            repo.start_run(),
            "failed",
            discovered=20,
            enriched=0,
            reported=0,
            error="источник отдал 18 страниц вакансий, не разобрана ни одна",
        )

    result = invoke(config_dir, "healthcheck")

    assert result.exit_code == 1
    assert "последний прогон: failed" in result.output
    assert "найдено 20, обогащено 0, отправлено 0" in result.output
    assert "не разобрана ни одна" in result.output


def test_a_clock_jump_forward_does_not_make_healthcheck_green_forever(tmp_path: Path) -> None:
    """M1: строка журнала с датой в будущем гасила индикатор насовсем.

    Порог считается как «сейчас минус два интервала», и дата из будущего
    меньше его не бывает никогда. Достаточно одного скачка часов на
    хосте, чтобы healthcheck отвечал `ok` до самой этой даты — при
    сервисе, который к тому времени может не работать месяцами.
    """
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "ok", finished_at=datetime.now(UTC) + timedelta(days=400))

    result = invoke(config_dir, "healthcheck")

    assert result.exit_code == 1
    assert "последний успешный прогон: никогда" in result.output


# --- конфиг читается лениво ------------------------------------------------


def test_subcommand_help_works_without_any_config() -> None:
    """`--help` не имеет права требовать существующего /data/config.

    Пока конфиг грузил `@app.callback()`, любая подсказка по подкоманде
    падала на отсутствующем каталоге — то есть первый же способ разобраться
    с CLI не работал.
    """
    result = runner.invoke(app, ["--config-dir", "/nonexistent", "run", "--help"])
    assert result.exit_code == 0
    assert "прогон" in result.output


def test_missing_config_gives_a_message_and_not_a_traceback(tmp_path: Path) -> None:
    result = invoke(tmp_path / "nowhere", "healthcheck")
    assert result.exit_code == 2
    assert "не прочитан" in result.output
    assert "Traceback" not in result.output


def test_config_dir_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HH_CONFIG_DIR` читается в момент вызова, а не при импорте модуля."""
    config_dir = prepare(tmp_path)
    monkeypatch.setenv("HH_CONFIG_DIR", str(config_dir))
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hh.db").exists()


def test_unknown_sink_stops_the_run_before_the_database_is_touched(tmp_path: Path) -> None:
    """Опечатка в `sinks` роняет процесс на старте — контракт задачи 9.

    До сети и до `start_run()`: иначе за страницы уже заплачено запросами,
    а отчёт всё равно не выйдет.
    """
    broken = APP_YAML.replace("sinks: [csv, markdown]", "sinks: [csv, telegram]")
    config_dir = prepare(tmp_path, **{"app.yaml": broken})
    result = invoke(config_dir, "run")
    assert result.exit_code == 2
    assert "telegram" in result.output
    assert not (tmp_path / "state" / "hh.db").exists()


# --- run: код возврата повторяет статус прогона ---------------------------


@respx.mock
def test_run_writes_both_reports_and_exits_zero(tmp_path: Path) -> None:
    """Сквозной прогон через CLI: файлы отчётов на диске, код 0."""
    config_dir = prepare(tmp_path)
    mock_source()
    result = invoke(config_dir, "run")
    assert result.exit_code == 0
    csv_report = tmp_path / "reports" / f"{TODAY}-new.csv"
    assert "111" in csv_report.read_text(encoding="utf-8-sig")
    assert (tmp_path / "reports" / f"{TODAY}-new.md").exists()


@respx.mock
def test_run_exits_nonzero_when_reports_cannot_be_written(tmp_path: Path) -> None:
    """Главный сценарий I2: работа не сделана, а код возврата ноль.

    Каталог отчётов занят файлом, поэтому оба приёмника падают. Прогон
    обязан не помечать вакансии отправленными, понизить статус и отдать
    ненулевой код: cron про испорченный volume иначе не узнает никогда.

    Статус именно `failed`, а не `partial`: доставлено ноль при непустой
    очереди — это не частичная потеря, а отсутствие работы целиком, и
    `partial` считался бы для healthcheck успехом (см. C2 в
    `reporting._complain`).
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "reports").write_text("не каталог", encoding="utf-8")
    mock_source()
    result = invoke(config_dir, "run")
    assert result.exit_code == 1
    assert "failed" in result.output
    with SqliteRepository(state_path(config_dir)) as repo:
        assert [item.discovered.id for item in repo.unreported()] == ["111"]


@respx.mock
def test_healthcheck_goes_red_when_nothing_is_ever_delivered(tmp_path: Path) -> None:
    """C2 целиком, глазами Docker: сутки прогонов, ни одного отчёта.

    Точка входа контейнера — `serve`, кода возврата которого не видит
    никто, поэтому healthcheck остаётся единственным индикатором. Шесть
    прогонов подряд (сутки при `interval_hours: 4`) с недоступным томом
    отчётов обязаны оставить его красным.
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "reports").write_text("не каталог", encoding="utf-8")
    mock_source()
    for _ in range(6):
        assert invoke(config_dir, "run").exit_code == 1
    assert invoke(config_dir, "healthcheck").exit_code == 1


@respx.mock
def test_run_exits_one_when_the_source_is_silent(tmp_path: Path) -> None:
    """Пустая выдача по всем листингам — `failed` и код 1, а не тихий ноль."""
    config_dir = prepare(tmp_path)
    mock_source()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="canonical" href="/vacancies/programmist">'
            '<script type="application/ld+json">{"@type": "ItemList", '
            '"itemListElement": []}</script></head></html>',
        )
    )
    result = invoke(config_dir, "run")
    assert result.exit_code == 1
    assert "failed" in result.output


@respx.mock
def test_run_exits_one_on_forbidden(tmp_path: Path) -> None:
    """403 — остановка прогона и внятное сообщение, а не traceback (спека §9)."""
    config_dir = prepare(tmp_path)
    mock_source(listing_status=403)
    result = invoke(config_dir, "run")
    assert result.exit_code == 1
    assert "закрыл доступ" in result.output
    assert "Traceback" not in result.output
    # M2: текст исключения уже кончается словами «Прогон остановлен,
    # обходные пути не применяются», и CLI приклеивал их второй раз —
    # «…не применяются.. Обходные пути не применяются». Считается ровно
    # строка ответа команды, а не весь вывод: в логе то же правило
    # называется другими словами и по делу.
    answer = next(line for line in result.output.splitlines() if "закрыл доступ" in line)
    assert answer.lower().count("обходные пути не применяются") == 1
    assert ".." not in answer


# --- mark: id и статус вводит человек -------------------------------------


@respx.mock
def test_mark_sets_the_status_of_an_existing_vacancy(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    result = invoke(config_dir, "mark", "111", "applied")
    assert result.exit_code == 0
    assert read_status(state_path(config_dir), "111") == "applied"


def test_mark_fails_on_an_unknown_id(tmp_path: Path) -> None:
    """`rowcount`, а не «команда не упала».

    Прежняя редакция печатала «111 → applied» и отдавала ноль на любой
    выдуманный id: единственный способ узнать об опечатке — сходить в базу
    руками.
    """
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "mark", "999999", "applied")
    assert result.exit_code == 1
    assert "нет в базе" in result.output


@respx.mock
def test_mark_reported_keeps_the_vacancy_in_the_report(tmp_path: Path) -> None:
    """I2: `mark <id> reported` прятал вакансию из ОБОИХ путей отчёта.

    `unreported()` её больше не видит (там `status='new'`), а
    `reported_since()` не видел никогда: `reported_at` оставался `NULL`, а
    условие выборки — `reported_at >= ?`. Команда при этом отвечала
    «111 → reported» и кодом 0. Это тот же остаток, который коммит
    c7d4b4d закрыл для `mark X new`.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.set_status("111", "new")
    for report in (tmp_path / "reports").iterdir():
        report.unlink()

    assert invoke(config_dir, "mark", "111", "reported").exit_code == 0

    result = invoke(config_dir, "report", "--since", "7d")
    assert result.exit_code == 0
    assert "отдано приёмникам вакансий: 1" in result.output
    assert "111" in (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")


def test_mark_rejects_an_unknown_status(tmp_path: Path) -> None:
    """`set_status` статус не валидирует, а вводит его человек: опечатка
    (`aplied`) увела бы вакансию в состояние, невидимое всем трём выборкам."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "mark", "111", "aplied")
    assert result.exit_code == 2
    assert "допустимы" in result.output


# --- report: единственный способ вернуть историю --------------------------


@respx.mock
def test_report_regenerates_the_files_from_the_database(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    for report in (tmp_path / "reports").iterdir():
        report.unlink()

    result = invoke(config_dir, "report", "--since", "7d")
    assert result.exit_code == 0
    assert "отдано приёмникам вакансий: 1" in result.output
    assert "записано новых строк — csv: 1, markdown: 1" in result.output
    assert "111" in (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")


@respx.mock
def test_report_survives_a_corrupted_row(tmp_path: Path) -> None:
    """C5: `report` обязан переживать порчу базы ЛУЧШЕ конвейера, а не хуже.

    Одна строка с битым UTF-8 в `title` роняла весь курсор
    (`OperationalError: Could not decode to UTF-8 column 'title'`) — то
    есть единственный способ пользователя вернуть историю отнимался
    целиком. Здесь порча ровно та же, а вторая вакансия обязана доехать до
    отчёта.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="canonical" href="/vacancies/programmist">'
            '<script type="application/ld+json">{"@type": "ItemList", "itemListElement": ['
            '{"url": "https://hh.ru/vacancy/111", "name": "Embedded Engineer"},'
            '{"url": "https://hh.ru/vacancy/333", "name": "Linux Engineer"}]}'
            "</script></head></html>",
        )
    )
    invoke(config_dir, "run")
    db = str(state_path(config_dir))
    raw = sqlite3.connect(db)
    raw.execute("UPDATE vacancy SET title = CAST(? AS TEXT) WHERE id = '111'", (b"\xff\xfe",))
    raw.commit()
    raw.close()
    for report in (tmp_path / "reports").iterdir():
        report.unlink()

    result = invoke(config_dir, "report", "--since", "7")
    assert result.exit_code == 0
    assert "отдано приёмникам вакансий: 1" in result.output
    body = (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")
    assert "333" in body


@respx.mock
def test_report_says_zero_when_the_day_file_already_has_everything(tmp_path: Path) -> None:
    """M3: «перегенерировано вакансий: 143», не записав ни байта.

    Приёмники дедуплицируют по содержимому файла дня, поэтому повторный
    `report` за тот же день — законный ноль записей. Сообщение об этом
    молчало и называло число ОТДАННЫХ вакансий, отправляя человека искать
    изменения, которых нет: замер — размеры обоих файлов до и после
    совпадают до байта.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    reports = sorted((tmp_path / "reports").iterdir())
    before = [report.read_bytes() for report in reports]

    result = invoke(config_dir, "report", "--since", "7d")

    assert result.exit_code == 0
    assert "отдано приёмникам вакансий: 1" in result.output
    assert "записано новых строк — csv: 0, markdown: 0" in result.output
    assert [report.read_bytes() for report in reports] == before


def test_report_rejects_an_unparsable_period(tmp_path: Path) -> None:
    """`--since 7days` давал traceback: единственный признак — стектрейс."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "report", "--since", "7days")
    assert result.exit_code == 2
    assert "число дней" in result.output
    assert "Traceback" not in result.output


def test_report_says_when_there_is_nothing(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "report")
    assert result.exit_code == 0
    assert "не найдено" in result.output


# --- логи -----------------------------------------------------------------


@respx.mock
def test_every_command_writes_the_log_file(tmp_path: Path) -> None:
    """`setup_logging` вызывается не только в `run`/`serve`.

    Карантин пишет `ERROR` из `report` и `mark` тоже, и эти записи —
    единственный след порчи данных. Пока логирование настраивали два
    места из шести, они уходили в никуда.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    db = str(state_path(config_dir))
    raw = sqlite3.connect(db)
    raw.execute("UPDATE vacancy SET title = CAST(? AS TEXT) WHERE id = '111'", (b"\xff\xfe",))
    raw.commit()
    raw.close()
    # Корневой логгер сбрасывается ДО `report`, потому что в проде это
    # отдельный процесс с ненастроенным логированием. В одном процессе
    # pytest файловый обработчик остался бы висеть от предыдущего `run`, и
    # тест был бы зелен даже если бы `report` логи не настраивал вовсе, —
    # то есть сторожил бы ровно не то, ради чего написан.
    reset_root_logger()
    (tmp_path / "logs" / "hh.log").write_text("", encoding="utf-8")

    invoke(config_dir, "report", "--since", "7")
    assert "повреждены данные" in (tmp_path / "logs" / "hh.log").read_text(encoding="utf-8")


def reset_root_logger() -> None:
    for handler in logging.getLogger().handlers[:]:
        handler.close()
        logging.getLogger().removeHandler(handler)


def read_status(db: Path, vacancy_id: str) -> str | None:
    raw = sqlite3.connect(str(db))
    row = raw.execute("SELECT status FROM vacancy WHERE id = ?", (vacancy_id,)).fetchone()
    raw.close()
    return None if row is None else str(row[0])


# --- I2: два одновременных прогона не имеют права задваивать отчёт ---------


@respx.mock
def test_run_refuses_to_start_while_another_run_holds_the_lock(tmp_path: Path) -> None:
    """`docker exec … run` во время работы `serve` — естественное действие.

    Взаимного исключения не было ни в CLI, ни в хранилище, а дедупликация
    приёмника — read-modify-write по файлу дня, то есть гонка. Результат
    воспроизводился: 38 строк вместо 19, 18 дублей и второй BOM в
    СЕРЕДИНЕ файла, хотя приёмник обещает ровно один BOM за файл.

    Замок берётся здесь СЫРЫМ `fcntl`, а не через API проекта: тест
    обязан описывать поведение (второй прогон отказывается стартовать), а
    не устройство замка.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "init-db").exit_code == 0
    lock_path = state_path(config_dir).with_name(state_path(config_dir).name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = invoke(config_dir, "run")

    assert result.exit_code == 1
    assert "уже идёт" in result.output
    assert "Traceback" not in result.output
    assert runs_in_journal(state_path(config_dir)) == []


@respx.mock
def test_report_refuses_to_overwrite_the_file_of_a_running_run(tmp_path: Path) -> None:
    """`report` пишет в ТОТ ЖЕ файл дня, что и прогон.

    Дедупликация приёмника — чтение-правка-запись, поэтому `report`
    гонится с прогоном ровно так же, как два прогона между собой.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "run").exit_code == 0
    lock_path = state_path(config_dir).with_name(state_path(config_dir).name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = invoke(config_dir, "report", "--since", "7d")

    assert result.exit_code == 1
    assert "уже идёт" in result.output


@respx.mock
def test_the_lock_is_released_when_the_run_ends(tmp_path: Path) -> None:
    """Замок держится ровно на время прогона: следующий обязан пройти.

    Прошлый прогон отодвигается в прошлое, иначе второй не дошёл бы до
    замка вовсе: предохранитель от шторма перезапусков пропускает прогон,
    если предыдущий успешный был только что (см. `_too_soon`). Здесь
    предмет — замок, и мешать ему другой предохранитель не должен.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "run").exit_code == 0
    backdate_last_run(state_path(config_dir), hours=3)
    assert invoke(config_dir, "run").exit_code == 0
    assert len(runs_in_journal(state_path(config_dir))) == 2


def test_four_parallel_runs_leave_one_intact_report(tmp_path: Path) -> None:
    """Тот же сценарий по-настоящему: четыре `run` одновременно.

    Проверяются инварианты отчёта, а не порядок победителей: BOM ровно
    один на файл, id не задвоены, и ни одна строка журнала не осталась
    незакрытой. С замком это детерминировано. Без него окно гонки
    открывается не в каждом залпе (замер на FIX_BASE: испорченный отчёт в
    1 из 10 залпов — 38 строк вместо 19 и второй BOM в середине файла),
    поэтому дифференциальным сторожем механизма служит тест выше, а этот
    сторожит сам инвариант отчёта.
    """
    config_dir = prepare(tmp_path)
    driver = tmp_path / "driver.py"
    driver.write_text(PARALLEL_DRIVER, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(driver), str(config_dir)], check=True, cwd=REPO_ROOT, env=child_env()
    )
    processes = [
        subprocess.Popen(
            [sys.executable, str(driver), str(config_dir), "run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env=child_env(),
        )
        for _ in range(4)
    ]
    outputs = [process.communicate()[0].strip() for process in processes]

    csv_report = tmp_path / "reports" / f"{TODAY}-new.csv"
    body = csv_report.read_bytes()
    lines = body.decode("utf-8-sig").splitlines()
    ids = [line.split(";")[0] for line in lines[1:] if line]
    assert body.count("﻿".encode()) == 1, f"BOM не один: {outputs}"
    assert len(ids) == len(set(ids)), f"отчёт задвоен: {ids}"
    assert "111" in ids
    statuses = {status for status, _ in runs_in_journal(state_path(config_dir))}
    assert "running" not in statuses


REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Отдельный процесс, а не поток: замок прогона живёт в файловой системе, и
# проверять его имеет смысл только настоящей параллельностью процессов.
PARALLEL_DRIVER = f"""
import sys
sys.path.insert(0, {REPO_ROOT!r})
import httpx, respx
from pathlib import Path
from typer.testing import CliRunner
from hh_search.__main__ import app
from tests.test_pipeline import TWO_VACANCIES, page_html

FIXTURES = Path({REPO_ROOT!r}) / "tests" / "fixtures"
config_dir, *rest = sys.argv[1:]
command = rest[0] if rest else "init-db"
runner = CliRunner()
with respx.mock:
    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={{"Content-Type": "text/plain"}},
        )
    )
    respx.get(url__startswith="https://hh.ru/vacancies/programmist").mock(
        return_value=httpx.Response(200, text=TWO_VACANCIES)
    )
    respx.get(url__regex=r"^https://hh\\.ru/vacancy/\\d+$").mock(
        return_value=httpx.Response(200, text=page_html())
    )
    print(runner.invoke(app, ["--config-dir", config_dir, command]).exit_code)
"""


def child_env() -> dict[str, str]:
    """Окружение дочернего процесса: без кэша байткода и без ANSI в выводе."""
    return {**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def runs_in_journal(db: Path) -> list[tuple[str, object]]:
    raw = sqlite3.connect(str(db))
    rows = raw.execute("SELECT status, reported FROM run ORDER BY id").fetchall()
    raw.close()
    return [(str(row[0]), row[1]) for row in rows]


# --- M1: `report` обязан отказывать так же, как `run` ---------------------


@respx.mock
def test_report_exits_partial_when_sinks_cannot_write(tmp_path: Path) -> None:
    """Одна и та же беда — один и тот же ответ CLI.

    Недоступный каталог отчётов давал здесь голый traceback, тогда как
    `run` в этой же ситуации отдаёт `partial`, код 3 и внятный текст. По
    выводу `report` человек решает, чинить ему конфиг или том, — а
    стектрейс на этот вопрос не отвечает.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "run").exit_code == 0
    reports = tmp_path / "reports"
    for report in reports.iterdir():
        report.unlink()
    reports.rmdir()
    reports.write_text("не каталог", encoding="utf-8")

    result = invoke(config_dir, "report", "--since", "7d")

    assert result.exit_code == 3
    assert "не приняли отчёт" in result.output
    # Стектрейс в выводе быть может — его печатает логгер приёмника
    # (`exc_info=True`), как и в `run`. Чего быть не должно, так это
    # необработанного исключения: именно оно давало код 1 и ни слова о том,
    # что случилось.
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --- M2: строка `running` от убитого процесса не имеет права висеть вечно --


@respx.mock
def test_a_run_row_left_by_a_killed_process_is_closed(tmp_path: Path) -> None:
    """После SIGKILL строка остаётся `running` навсегда и никем не показана.

    Успешным такой прогон не считался и раньше, но кладбище росло без
    единого признака. Закрывается оно под замком прогона: пока замок наш,
    любая строка `running` заведомо принадлежит мёртвому процессу.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "init-db").exit_code == 0
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.start_run()
    assert [status for status, _ in runs_in_journal(state_path(config_dir))] == ["running"]

    assert invoke(config_dir, "run").exit_code == 0

    statuses = [status for status, _ in runs_in_journal(state_path(config_dir))]
    assert statuses == ["interrupted", "ok"]
    assert invoke(config_dir, "healthcheck").exit_code == 0


# --- M7: устойчивый 403 обязан прекращать запросы в описанном развёртывании -


def test_persistent_forbidden_leaves_a_marker_that_stops_the_next_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`restart: unless-stopped` перезапускает контейнер на ЛЮБОМ коде.

    Значит «демон остановлен кодом 1» в описанном спекой развёртывании
    недостижимо: счётчик подряд идущих 403 обнуляется вместе с процессом,
    и контейнер стучится к hh.ru вечно. Маркер достигает того, ради чего
    остановка и требуется, — ни одного запроса до вмешательства человека.
    """
    config_dir = prepare(tmp_path)
    assert invoke(config_dir, "init-db").exit_code == 0
    monkeypatch.setattr("hh_search.__main__.serve", lambda *args, **kwargs: 1, raising=True)
    assert invoke(config_dir, "serve").exit_code == 1
    marker = tmp_path / "state" / "access-forbidden.stop"
    assert marker.exists()

    calls: list[object] = []

    def remember(*args: object, **kwargs: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr("hh_search.__main__.serve", remember, raising=True)
    result = invoke(config_dir, "serve")

    assert result.exit_code == 1
    assert "маркер" in result.output
    assert calls == [], "демон обязан не начинать работу, пока стоит маркер"


# --- serve: точка входа контейнера, до сих пор не вызванная ни одним тестом -


class OneShotStop(StopSignal):
    """Останавливается сразу: цикл делает ровно один прогон и выходит.

    Подменяет `StopSignal` внутри команды, а не саму `serve`: проверять
    надо именно проводку `serve_command` — она и есть точка входа Docker.
    """

    def install(self, numbers: tuple[signal.Signals, ...] = ()) -> None:
        self.request()


@respx.mock
def test_serve_runs_the_pipeline_and_writes_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сквозной прогон через `serve` — тот же, что делает контейнер."""
    config_dir = prepare(tmp_path)
    mock_source()
    monkeypatch.setattr("hh_search.__main__.StopSignal", OneShotStop)

    result = invoke(config_dir, "serve")

    assert result.exit_code == 0
    assert "111" in (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")
    assert [status for status, _ in runs_in_journal(state_path(config_dir))] == ["ok"]


def test_serve_builds_sinks_before_the_loop_and_not_inside_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Опечатка в `sinks` обязана ронять `serve` на старте.

    Собранный ВНУТРИ прогона неизвестный приёмник дал бы `ValueError`,
    который планировщик глотает своим `except Exception`, — и демон
    крутил бы бесполезный цикл каждые четыре часа, отвечая нулевым кодом
    и ничего не отправляя. Контракт задачи 9 держится только на порядке
    вызовов, поэтому сторожить надо именно его.
    """
    broken = APP_YAML.replace("sinks: [csv, markdown]", "sinks: [csv, telegram]")
    config_dir = prepare(tmp_path, **{"app.yaml": broken})
    monkeypatch.setattr("hh_search.__main__.StopSignal", OneShotStop)

    result = invoke(config_dir, "serve")

    assert result.exit_code == 2
    assert "telegram" in result.output
    assert not (tmp_path / "state" / "hh.db").exists()


@pytest.mark.parametrize("command", ["run", "report"])
def test_missing_telegram_secrets_do_not_blame_app_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Сообщение об отказе обязано называть причину, которая есть.

    Прежний текст — «в app.yaml неизвестный приёмник: приёмник telegram
    включён, но не задано: TELEGRAM_CHAT_ID» — врал дважды: приёмник
    известен, и `app.yaml` тут ни при чём. Человек шёл править не тот
    файл. Ошибка `build_sinks` объясняет себя сама, префикс ей не нужен.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    broken = APP_YAML.replace("sinks: [csv, markdown]", "sinks: [csv, telegram]")
    config_dir = prepare(tmp_path, **{"app.yaml": broken})

    result = invoke(config_dir, command)

    assert result.exit_code == 2
    assert "TELEGRAM_BOT_TOKEN" in result.output
    assert "app.yaml" not in result.output
    assert "неизвестный приёмник" not in result.output


# --- C2: маркер обязан останавливать и `run`, а не только `serve` ----------


@respx.mock
def test_run_sends_nothing_while_the_forbidden_marker_stands(tmp_path: Path) -> None:
    """«Перезапущенный контейнер не отправляет ни одного запроса» — контракт
    маркера из README, и проверялся он только в `serve`.

    Дырка открывалась самым естественным действием человека: увидев
    остановленный демон, он выполняет `docker compose run --rm hh-search
    run` («а сейчас как?») — и предохранитель, ради которого маркер и
    заведён, пропускает прогон целиком. Замер на FIX_BASE: 11 запросов к
    hh.ru при стоящем маркере.
    """
    config_dir = prepare(tmp_path)
    assert invoke(config_dir, "init-db").exit_code == 0
    marker = tmp_path / "state" / "access-forbidden.stop"
    marker.write_text("доступ закрыт устойчиво\n", encoding="utf-8")
    mock_source()

    result = invoke(config_dir, "run")

    assert result.exit_code == 1
    assert "маркер" in result.output
    assert "удалите" in result.output.lower()
    assert respx.calls.call_count == 0, "при стоящем маркере в сеть не уходит ничего"


# --- I2: перезапуск контейнера не имеет права означать немедленный прогон --


@respx.mock
def test_a_run_right_after_a_successful_one_is_skipped(tmp_path: Path) -> None:
    """`docker restart` — обычное действие владельца, а не команда «обойди hh.ru».

    Замер на FIX_BASE: `up -d` плюс три `restart` за 48 секунд — 41 запрос
    к hh.ru. Проверки «прошлый прогон был только что» не было нигде.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "run").exit_code == 0
    after_first = respx.calls.call_count

    result = invoke(config_dir, "run")

    assert result.exit_code == 0
    assert "пропущен" in result.output
    assert respx.calls.call_count == after_first, "повторный прогон не имеет права идти в сеть"


@respx.mock
def test_a_run_happens_again_once_the_previous_success_is_old_enough(tmp_path: Path) -> None:
    """Контроль к предыдущему тесту: предохранитель не имеет права
    подменять собой расписание и глушить прогоны навсегда."""
    config_dir = prepare(tmp_path)
    mock_source()
    assert invoke(config_dir, "run").exit_code == 0
    after_first = respx.calls.call_count
    backdate_last_run(state_path(config_dir), hours=3)

    assert invoke(config_dir, "run").exit_code == 0

    assert respx.calls.call_count > after_first
    assert len(runs_in_journal(state_path(config_dir))) == 2


@respx.mock
def test_a_burst_of_restarts_costs_one_run_and_not_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же сценарий глазами Docker: `up -d` и три `restart` подряд.

    `restart: unless-stopped` плюс любая падающая конфигурация — это
    множитель запросов, а обычная возня владельца («поправил профиль —
    перезапустил») стоила бы десятков лишних обходов листингов за вечер.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    monkeypatch.setattr("hh_search.__main__.StopSignal", OneShotStop)

    codes = [invoke(config_dir, "serve").exit_code for _ in range(4)]
    after_first_run = 1 + 2 + 1  # robots.txt + две страницы листинга + одна вакансия

    assert codes == [0, 0, 0, 0]
    assert respx.calls.call_count == after_first_run
    assert [status for status, _ in runs_in_journal(state_path(config_dir))] == ["ok"]


def backdate_last_run(db: Path, hours: int) -> None:
    """Отодвинуть последний прогон в прошлое: расписание тестом не ждут."""
    stale = datetime.now(UTC) - timedelta(hours=hours)
    raw = sqlite3.connect(str(db))
    raw.execute(
        "UPDATE run SET finished_at = ? WHERE id = (SELECT MAX(id) FROM run)",
        (stale.isoformat(),),
    )
    raw.commit()
    raw.close()


# --- I3: отказ тома обязан называть причину, а не показывать traceback -----
#
# Самая вероятная ошибка первого дня на VPS: uid контейнера не совпадает с
# владельцем тома, либо том смонтирован `:ro`. Замер на FIX_BASE: `init-db`
# от чужого uid — 22 строки rich-трейсбека, `run` на созданной базе — 52,
# том `:ro` — 52. Контраст с недоступным каталогом ОТЧЁТОВ, обработанным
# образцово (статус `failed`, понятная причина, вакансии сохранены), и
# делал эту ветку заметной.


def deny_writes(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)


def allow_writes(path: Path) -> None:
    path.chmod(0o755)


def test_init_db_without_write_access_explains_the_uid(tmp_path: Path) -> None:
    """`init-db` в каталог, куда процессу не пишут: одна строка, не traceback."""
    config_dir = prepare(tmp_path)
    deny_writes(tmp_path)
    try:
        result = invoke(config_dir, "init-db")
    finally:
        allow_writes(tmp_path)

    assert result.exit_code == 1
    assert "нет доступа к каталогу данных" in result.output
    assert "uid" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@respx.mock
def test_run_on_a_read_only_volume_explains_the_uid(tmp_path: Path) -> None:
    """База создана, том стал только на чтение: падал замок прогона."""
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "init-db")
    state = tmp_path / "state"
    deny_writes(state)
    try:
        result = invoke(config_dir, "run")
    finally:
        allow_writes(state)

    assert result.exit_code == 1
    assert "нет доступа к каталогу данных" in result.output
    assert ".env" in result.output or "chmod" in result.output
    assert "Traceback" not in result.output


@respx.mock
def test_mark_on_a_read_only_volume_explains_the_uid(tmp_path: Path) -> None:
    """Та же беда из любой команды: разные ответы на один отказ — не мелочь.

    Вакансия обязана существовать: `UPDATE`, не задевший ни одной строки,
    sqlite выполняет, не тронув файл, и такой `mark` отвечает «нет в базе»
    даже на томе `:ro` — то есть проверял бы не тот отказ.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    state = tmp_path / "state"
    deny_writes(state)
    try:
        result = invoke(config_dir, "mark", "111", "applied")
    finally:
        allow_writes(state)

    assert result.exit_code == 1
    assert "нет доступа к каталогу данных" in result.output
    assert "Traceback" not in result.output


@pytest.mark.skipif(os.getuid() == 0, reason="от root чужого владельца не бывает")
def test_a_foreign_owner_is_named_and_hh_uid_is_suggested(tmp_path: Path) -> None:
    """Чужой владелец каталога — тот самый случай из `.env.example`.

    Совет обязан отличаться от совета при СВОЁМ uid: правка `HH_UID` при
    совпадающем владельце не лечит ничего, а человек уже поверил, что дело
    в ней.
    """
    config_dir = prepare(tmp_path, **{"app.yaml": read_only_root_config(tmp_path)})
    result = invoke(config_dir, "init-db")

    assert result.exit_code == 1
    assert "HH_UID" in result.output
    assert "принадлежит uid=0" in result.output
    assert "Traceback" not in result.output


def read_only_root_config(tmp_path: Path) -> str:
    """Конфиг, чей каталог состояния лежит под каталогом чужого владельца.

    `/usr` принадлежит root и обычному пользователю не пишется, поэтому
    чужой владелец здесь настоящий, а не сымитированный chmod'ом: ветка
    сообщения про `HH_UID` проверяется ровно тем, ради чего написана.
    """
    return (
        APP_YAML.replace("delay_between_requests_sec: 1.0", "delay_between_requests_sec: 0.1")
        .replace("/data/state", "/usr/hh-search-state")
        .replace("/data/reports", str(tmp_path / "reports"))
        .replace("/data/logs", str(tmp_path / "logs"))
    )


# --- I2: усечение отчёта потолком обязано быть сказано вслух ---------------


@respx.mock
def test_report_says_out_loud_that_it_was_truncated(tmp_path: Path) -> None:
    """Неполный отчёт, выданный за полный, — та самая тихая потеря.

    Потолок здесь лечит замеренный OOM (`report --since 60` на базе с
    22 000 вакансий — 351 МБ RSS, то есть смерть процесса по команде
    человека на VPS с 512 МБ). Но усечение видит ЧЕЛОВЕК, а не следующий
    прогон, поэтому молчать о нём нельзя: отбор идёт по убыванию оценки,
    и за границей всегда остаётся хвост.
    """
    config_dir = prepare(tmp_path)
    app_file = config_dir / "app.yaml"
    app_file.write_text(
        app_file.read_text(encoding="utf-8") + "limits:\n  rows_per_batch: 1\n", encoding="utf-8"
    )
    mock_source()
    invoke(config_dir, "run")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.mark_reported(["111", "222"])

    result = invoke(config_dir, "report", "--since", "7d")

    assert result.exit_code == 0
    assert "отчёт усечён потолком app.limits.rows_per_batch = 1" in result.output


@respx.mock
def test_a_report_that_fits_says_nothing_about_truncation(tmp_path: Path) -> None:
    """Контроль: предупреждение не имеет права быть постоянным фоном."""
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.mark_reported(["111"])

    result = invoke(config_dir, "report", "--since", "7d")

    assert "усечён" not in result.output


# --- M11: цветной трейсбек rich выключен ----------------------------------


def test_cli_does_not_print_rich_tracebacks() -> None:
    """Одна ошибка на 20–50 строк с рамками — худшая форма для `docker logs`.

    Зависимость от rich так не снимается (её тянет сам typer, ~13 МБ
    образа), и снять её нечем, кроме отказа от typer. Выключается именно
    оформление трейсбека — то, что мешает читать журнал сервиса.
    """
    assert app.pretty_exceptions_enable is False
