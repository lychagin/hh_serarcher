import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from hh_search.__main__ import app
from hh_search.config.loader import load_config
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
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "reports").write_text("не каталог", encoding="utf-8")
    mock_source()
    result = invoke(config_dir, "run")
    assert result.exit_code == 3
    assert "partial" in result.output
    with SqliteRepository(state_path(config_dir)) as repo:
        assert [item.discovered.id for item in repo.unreported()] == ["111"]


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
    assert "перегенерировано вакансий: 1" in result.output
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
    assert "перегенерировано вакансий: 1" in result.output
    body = (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")
    assert "333" in body


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
