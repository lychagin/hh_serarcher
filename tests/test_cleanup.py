"""Уборка: план, исполнение и то, чего она не трогает никогда."""

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hh_search.domain.models import DiscoveredVacancy, Salary, ScoreBreakdown, VacancyDetails
from hh_search.pipeline.cleanup import (
    HORIZON_CLOCK_SKEW_TOLERANCE,
    HORIZON_FILE,
    PROTECTED_DAYS,
    CleanupDays,
    CleanupPlan,
    execute,
    horizon,
    plan,
)
from hh_search.sinks.telegram_sink import LOOKBACK_DAYS
from hh_search.storage.repository import SqliteRepository
from hh_search.storage.time_utils import to_utc_iso

NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def make_vacancy(vacancy_id: str = "1") -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=vacancy_id,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
        title="Embedded Linux Engineer",
        company="ООО Ромашка",
        area="Нижний Новгород",
        salary=Salary(raw="от 200 000 руб.", amount_from=200000, currency="руб."),
        published_at=datetime(2026, 7, 27, 9, 0, 0),
        found_by_query="Yocto",
    )


def make_score(total: float = 87.4) -> ScoreBreakdown:
    return ScoreBreakdown(
        title=1.0,
        stack=0.8,
        responsibilities=0.67,
        domain=1.0,
        penalty=0.0,
        total=total,
        matched={"stack": ["Yocto"]},
    )


def _backdate_reported_at(repo: SqliteRepository, moment: datetime, *vacancy_ids: str) -> None:
    """`reported_at` в прошлом — публичный API такой даты не ставит (см. `test_repository.py`)."""
    repo._connection.execute(  # noqa: SLF001 — reported_at в прошлом публичным API не поставить
        f"UPDATE vacancy SET reported_at = ? WHERE id IN ({','.join('?' for _ in vacancy_ids)})",
        (to_utc_iso(moment), *vacancy_ids),
    )
    repo._connection.commit()  # noqa: SLF001 — та же подготовка состояния


def report_file(reports: Path, day: date, suffix: str = "-new.html") -> Path:
    path = reports / f"{day:%Y-%m-%d}{suffix}"
    path.write_text("<html>отчёт</html>", encoding="utf-8")
    return path


def test_plan_changes_nothing_on_disk(tmp_path: Path) -> None:
    """Сухой прогон обязан быть сухим — сверка побайтно.

    Для необратимой команды это единственный сторож, который отличает
    «показал план» от «сделал и рассказал».
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    report_file(reports, date(2020, 1, 1))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2020, 1, 1, tzinfo=UTC), "1")

    before = {path: path.read_bytes() for path in sorted(reports.iterdir())}
    db_before = db.read_bytes()

    result = plan(repo, reports, NOW, CleanupDays(reports=90))

    assert result.descriptions == 1
    assert {path: path.read_bytes() for path in sorted(reports.iterdir())} == before
    assert db.read_bytes() == db_before


def test_execute_clears_descriptions_and_removes_old_report_files(tmp_path: Path) -> None:
    """Исполнение делает ровно то, что обещал план."""
    reports = tmp_path / "reports"
    reports.mkdir()
    old = report_file(reports, date(2020, 1, 1))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2020, 1, 1, tzinfo=UTC), "1")
    state = tmp_path / "state"

    result = execute(repo, reports, state, NOW, CleanupDays(reports=90))

    assert result.descriptions == 1
    assert not old.exists()
    # описание действительно обнулено в базе, а не только в возвращённом числе
    assert repo.descriptions_before(NOW) == (0, 0)


def test_report_files_are_kept_without_the_reports_flag(tmp_path: Path) -> None:
    """Без флага файлы целы: удалённый отчёт не восстановит ничто.

    Описание всегда можно перекачать с hh.ru, строку журнала не жалко, а
    файл — единственное необратимое из трёх, и оно не должно случаться
    заодно с двумя обратимыми.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    report_file(reports, date(2020, 1, 1))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"

    execute(repo, reports, state, NOW, CleanupDays(reports=None))
    assert (reports / "2020-01-01-new.html").exists()


def test_the_redelivery_window_survives_reports_days_zero(tmp_path: Path) -> None:
    """Файлы дня и отметки за окно довозки не удаляются даже при сроке 0.

    Удалённая отметка `.sent` означает «документ застрял», и следующий
    прогон отправил бы его в канал повторно. Сторож привязан к
    `LOOKBACK_DAYS`, а не к числу: разъедься они — и уборка начала бы
    ломать довозку молча.
    """
    assert PROTECTED_DAYS >= LOOKBACK_DAYS + 1
    reports = tmp_path / "reports"
    reports.mkdir()
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"
    for offset in range(LOOKBACK_DAYS + 1):
        day = NOW.date() - timedelta(days=offset)
        report_file(reports, day)
        report_file(reports, day, suffix="-new.html.sent")
    ancient = report_file(reports, date(2020, 1, 1))

    execute(repo, reports, state, NOW, CleanupDays(reports=0))

    assert not ancient.exists()
    assert len(sorted(reports.iterdir())) == 2 * (LOOKBACK_DAYS + 1)


def test_files_without_a_date_prefix_are_never_touched(tmp_path: Path) -> None:
    """Чужие файлы и черновики уборка не трогает.

    За черновики `*.part` отвечает `TelegramSink.maintain`. Два
    механизма, убирающие одни и те же файлы, однажды разойдутся в том,
    кто из них главный.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"
    draft = reports / "2020-01-01-new.htmlABC.part"
    draft.write_text("черновик", encoding="utf-8")
    (reports / "README.txt").write_text("не трогать", encoding="utf-8")
    execute(repo, reports, state, NOW, CleanupDays(reports=0))
    assert draft.exists()
    assert (reports / "README.txt").exists()


def test_horizon_is_written_by_execute_and_read_back(tmp_path: Path) -> None:
    """Горизонт уборки — факт на диске, а не догадка.

    Без него потеря была бы тихой: выборки отчёта фильтруют по
    `description IS NOT NULL`, и `report --since 120d` после уборки на 90
    днях молча показал бы 90.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"
    execute(repo, reports, state, NOW, CleanupDays(descriptions=90))
    assert horizon(state) == date(2026, 5, 3)


def test_an_unreadable_horizon_reads_as_absent(tmp_path: Path) -> None:
    """Мусор в файле горизонта не роняет `report`.

    Предупреждение, которое роняет команду, хуже отсутствующего
    предупреждения.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "last-cleanup").write_text("не дата", encoding="utf-8")
    assert horizon(state) is None


# --- Сторожа контроллера: отрицательный срок, состав множества, часы -------


@pytest.mark.parametrize("field", ["descriptions", "runs", "reports"])
def test_cleanup_days_rejects_a_negative_horizon(field: str) -> None:
    """Отрицательный срок хранения обязан отвергаться на каждом из трёх полей.

    Между `--descriptions-days -365` и «стереть всё» сейчас не стояло бы
    ничего: граница в будущем (`now - timedelta(days=-365)`) обнуляет
    описание вакансии, отправленной секундой раньше. Проверка стоит в
    `CleanupDays`, а не в CLI, — так её получит любой вызывающий.
    """
    with pytest.raises(ValueError, match=field):
        CleanupDays(**{field: -1})


def test_cleanup_days_allows_zero_on_every_field() -> None:
    """Ноль — законное решительное значение, а не порча ввода.

    `--descriptions-days 0` и `CleanupDays(reports=0)` (см. сторожа окна
    довозки выше) обязаны конструироваться без ошибки: ноль — предельно
    короткий, но осмысленный срок хранения, не то же самое, что
    отрицательный.
    """
    CleanupDays(descriptions=0, runs=0, reports=0)


def test_plan_and_execute_select_the_same_set_on_a_mixed_pile(tmp_path: Path) -> None:
    """Сухой прогон и исполнение обязаны отбирать РОВНО одно множество.

    Не «одно число» — одно множество: те же вакансии, те же строки
    журнала, те же файлы. Смешанный набор — валидная старая вакансия и
    валидная свежая, старый и свежий прогон в журнале, два по-настоящему
    старых файла отчёта, файл внутри защищённого окна довозки, файл
    сегодняшнего дня и файл без даты в имени — построен так, чтобы
    неверный выбор ЛЮБОГО из трёх множеств изменил состав, а не только
    счётчик, и разошёлся бы с тем, что план пообещал. Расхождение сухого
    прогона с реальной уборкой — это ложь человеку о необратимом
    действии, и ровно этот класс дефекта уже находило ревью предыдущей
    задачи.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    ancient1 = report_file(reports, date(2020, 1, 1))
    ancient2 = report_file(reports, date(2021, 6, 15))
    protected = report_file(reports, date(2026, 7, 30))
    today_file = report_file(reports, NOW.date())
    dateless = reports / "README.txt"
    dateless.write_text("не трогать", encoding="utf-8")

    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()

    repo.add_discovered(make_vacancy("старая"), "embedded", 9)
    repo.save_enriched("старая", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["старая"])
    _backdate_reported_at(repo, datetime(2020, 1, 1, tzinfo=UTC), "старая")

    repo.add_discovered(make_vacancy("свежая"), "embedded", 9)
    repo.save_enriched("свежая", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["свежая"])

    old_run = repo.start_run()
    repo.finish_run(old_run, "ok", finished_at=datetime(2020, 1, 1, tzinfo=UTC))
    recent_run = repo.start_run()
    repo.finish_run(recent_run, "ok", finished_at=NOW)

    state = tmp_path / "state"
    days = CleanupDays(reports=0)

    expected = plan(repo, reports, NOW, days)
    assert expected.descriptions == 1
    assert expected.runs == 1
    assert expected.report_files == 2

    result = execute(repo, reports, state, NOW, days)

    assert (result.descriptions, result.runs, result.report_files) == (
        expected.descriptions,
        expected.runs,
        expected.report_files,
    )
    assert not ancient1.exists()
    assert not ancient2.exists()
    remaining_files = {path.name for path in reports.iterdir()}
    assert remaining_files == {protected.name, today_file.name, dateless.name}

    old_cutoff = datetime(2000, 1, 1, tzinfo=UTC)
    reported = [item.discovered.id for item in repo.reported_since(old_cutoff)]
    assert reported == ["свежая"]  # «старая» лишилась описания и выпала из этой выборки

    remaining_runs = {
        int(row["id"])
        for row in repo._connection.execute("SELECT id FROM run").fetchall()  # noqa: SLF001
    }
    assert remaining_runs == {recent_run}


def test_protected_window_is_computed_from_the_now_argument_not_the_system_clock(
    tmp_path: Path,
) -> None:
    """Защищённое окно отсчитывается от `now`-аргумента, а не от системных часов.

    `now` теста заведомо не совпадает с реальной датой машины. Файл
    датирован РЕАЛЬНЫМ сегодня — если бы граница защищённого окна
    считалась от `date.today()`, а не от переданного `now`, такой файл
    остался бы защищён на годы дольше, чем обещает контракт.

    M-4 (ревью Task 4, раунд 1): прежняя редакция хардкодила ОБЕ даты
    (`2030-06-15` и «сегодня» неявно, через `date.today()`) — приём с
    `date.today()` был правильным, но вторая дата обязана ехать ЗА первой,
    а не стоять числом: как только реальные часы дошли бы до
    `2030-06-12` (окно защиты трое суток до `2030-06-15`), тест начал бы
    краснеть от календаря, а не от кода — тот самый класс отказа, который
    и проверяет этот тест. `future_now` теперь считается смещением от
    `date.today()`, а не абсолютной датой, поэтому не протухает никогда.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    far_future = date.today() + timedelta(days=3650)
    future_now = datetime(far_future.year, far_future.month, far_future.day, 10, 0, tzinfo=UTC)
    near_real_today = report_file(reports, date.today())
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"

    execute(repo, reports, state, future_now, CleanupDays(reports=0))

    assert not near_real_today.exists()


def test_report_file_exactly_at_the_reports_cutoff_is_kept(tmp_path: Path) -> None:
    """Файл РОВНО на границе `reports`-срока не удаляется, только более старый.

    `reports=10` взят больше `PROTECTED_DAYS`, чтобы отвязать эту проверку
    от защищённого окна довозки: граница здесь — исключительно срок
    хранения. Обнаружено сравнением исполнением (мутация `day > cutoff`
    вместо `day >= cutoff` осталась незамеченной всеми прочими тестами
    файла, потому что в них граница `reports` всегда совпадала с
    защищённым окном): без отдельной проверки включительности границы
    вакансия, ровно достигшая срока хранения, удалялась бы на день раньше
    обещанного.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    at_cutoff = report_file(reports, NOW.date() - timedelta(days=10))
    one_day_older = report_file(reports, NOW.date() - timedelta(days=11))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"

    execute(repo, reports, state, NOW, CleanupDays(reports=10))

    assert at_cutoff.exists()
    assert not one_day_older.exists()


# --- Раунд починки 1 (ревью Task 4): I-1..I-5, M-1 -------------------------


def _prepare_pending_cleanup(tmp_path: Path) -> tuple[SqliteRepository, Path, Path]:
    """Старый файл отчёта и старая отправленная вакансия — общая заготовка
    для сторожей записи горизонта (F2/F5/K2): то, что уборка обязана
    сделать ДО попытки записать горизонт, и что не должно теряться, если
    сама запись не удастся.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    old = report_file(reports, date(2020, 1, 1))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2020, 1, 1, tzinfo=UTC), "1")
    return repo, reports, old


def _assert_destructive_steps_survived_horizon_failure(
    repo: SqliteRepository, old: Path, result: CleanupPlan
) -> None:
    """Файлы убраны, описание обнулено, VACUUM прошёл — несмотря на то, что
    записать горизонт не удалось. Отказ назван словами в `result.errors`."""
    assert not old.exists()
    assert repo.descriptions_before(NOW) == (0, 0)
    assert result.errors


# --- I-1: недоступный каталог отчётов не роняет уборку целиком -------------


def test_execute_survives_an_unreadable_reports_dir(tmp_path: Path) -> None:
    """I-1: каталог отчётов без прав доступа не должен ронять уборку целиком.

    Прецедент в этом же проекте: `TelegramSink._sweep_orphaned_drafts`
    глушит `OSError` ровно ради каталога `0o500` — «недоступный на запись
    каталог отчётов не имеет права ронять `maintain`». Уборка обязана
    вести себя так же: убрать базу и назвать отказ файлов словами, а не
    упасть трейсбеком.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    report_file(reports, date(2020, 1, 1))
    reports.chmod(0o000)
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    repo.save_enriched("1", VacancyDetails(description="Yocto"), make_score())
    repo.mark_reported(["1"])
    _backdate_reported_at(repo, datetime(2020, 1, 1, tzinfo=UTC), "1")
    state = tmp_path / "state"

    try:
        result = execute(repo, reports, state, NOW, CleanupDays(reports=90))
    finally:
        reports.chmod(0o755)  # иначе pytest не уберёт tmp_path следом

    assert result.report_files == 0
    assert result.errors
    assert result.descriptions == 1  # база убрана, несмотря на недоступные файлы
    assert horizon(state) is not None  # горизонт тоже не заблокирован отказом файлов


def test_plan_and_execute_survive_reports_dir_being_a_plain_file(tmp_path: Path) -> None:
    """I-1 (вторая форма): `reports_dir` — файл, а не каталог.

    `NotADirectoryError` из `iterdir()` не зависит от прав доступа и от
    того, кем запущен процесс (в отличие от `chmod 0o000`, который root
    игнорирует), поэтому проверяет тот же класс отказа надёжнее.
    """
    reports = tmp_path / "reports"
    reports.write_text("не каталог", encoding="utf-8")
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()

    plan_result = plan(repo, reports, NOW, CleanupDays(reports=90))
    assert plan_result.report_files == 0
    assert plan_result.errors

    state = tmp_path / "state"
    exec_result = execute(repo, reports, state, NOW, CleanupDays(reports=90))
    assert exec_result.report_files == 0
    assert exec_result.errors


# --- I-2: гонка между iterdir() и stat()/unlink() ---------------------------


def test_execute_survives_a_file_vanishing_between_listing_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (C1): жертва исчезает между `iterdir()` и `stat()`/`unlink()`.

    Гонка с внешним удалением, ротацией или синхронизацией тома. Уборка
    обязана пропустить пропавший файл и довести дело до конца — удалить
    оставшихся жертв, убрать базу, записать горизонт, — а не упасть
    посередине списка.

    Первый `stat()` пути `doomed` — тот, что делает `is_file()` внутри
    отбора жертв, — обязан пройти как обычно: иначе файл выпал бы из
    списка жертв ДО гонки и тест проверял бы не ту дыру. Гонка
    имитируется на ВТОРОМ обращении — том самом, что раньше стояло
    снаружи `try`.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    doomed = report_file(reports, date(2020, 1, 1))
    survivor = report_file(reports, date(2020, 1, 2))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"

    real_stat = Path.stat
    seen = {"count": 0}

    def flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == doomed:
            seen["count"] += 1
            if seen["count"] > 1:
                doomed.unlink(missing_ok=True)
                raise FileNotFoundError(doomed)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = execute(repo, reports, state, NOW, CleanupDays(reports=90))

    assert not survivor.exists()
    assert result.report_files == 1
    assert result.errors
    assert horizon(state) is not None


def test_plan_survives_a_file_vanishing_before_its_size_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2 (C2): та же гонка в сухом прогоне.

    Предпросмотр необратимой команды не имеет права падать сильнее самой
    команды — иначе `plan()` менее надёжен, чем `--apply`, и предпросмотр
    перестаёт быть предпросмотром именно тогда, когда нужнее всего.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    doomed = report_file(reports, date(2020, 1, 1))
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()

    real_stat = Path.stat
    seen = {"count": 0}

    def flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == doomed:
            seen["count"] += 1
            if seen["count"] > 1:
                doomed.unlink(missing_ok=True)
                raise FileNotFoundError(doomed)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = plan(repo, reports, NOW, CleanupDays(reports=90))

    assert result.report_files == 1
    assert result.errors


# --- I-3: запись горизонта после точки невозврата не защищена --------------


def test_horizon_write_failure_is_reported_not_raised_when_state_parent_is_read_only(
    tmp_path: Path,
) -> None:
    """F5: каталог-родитель состояния недоступен на запись.

    Горизонт не записывается, но остальная уборка (файлы, описания,
    VACUUM) уже случилась и не теряется. Писатель горизонта раньше не
    ловил ничего, хотя стоит ПОСЛЕ точки невозврата: человек видел
    трейсбек, не зная, случилась ли уборка вообще.
    """
    repo, reports, old = _prepare_pending_cleanup(tmp_path)
    state_parent = tmp_path / "readonly"
    state_parent.mkdir()
    state = state_parent / "state"
    state_parent.chmod(0o500)
    try:
        result = execute(repo, reports, state, NOW, CleanupDays(reports=90))
    finally:
        state_parent.chmod(0o700)  # иначе pytest не уберёт tmp_path следом

    _assert_destructive_steps_survived_horizon_failure(repo, old, result)
    assert horizon(state) is None


def test_horizon_write_failure_is_reported_not_raised_when_last_cleanup_is_a_directory(
    tmp_path: Path,
) -> None:
    """F2: `state/last-cleanup` оказался каталогом.

    `write_text` на пути, который уже существует как каталог, поднимает
    `IsADirectoryError` — уже удалённый файл отчёта от этого не
    возвращается, и исключение не имеет права уйти наружу.
    """
    repo, reports, old = _prepare_pending_cleanup(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / HORIZON_FILE).mkdir()

    result = execute(repo, reports, state, NOW, CleanupDays(reports=90))

    _assert_destructive_steps_survived_horizon_failure(repo, old, result)


def test_horizon_write_failure_is_reported_not_raised_when_state_dir_is_a_file(
    tmp_path: Path,
) -> None:
    """K2: `state_dir` сам оказался файлом, а не каталогом.

    `mkdir(parents=True, exist_ok=True)` всё равно поднимает
    `FileExistsError`, когда по этому пути уже лежит файл, — `exist_ok`
    прощает существующий КАТАЛОГ, не любой существующий узел.
    """
    repo, reports, old = _prepare_pending_cleanup(tmp_path)
    state = tmp_path / "state"
    state.write_text("не каталог", encoding="utf-8")

    result = execute(repo, reports, state, NOW, CleanupDays(reports=90))

    _assert_destructive_steps_survived_horizon_failure(repo, old, result)


# --- I-4: горизонт обязан быть монотонным -----------------------------------


def test_horizon_never_moves_backward(tmp_path: Path) -> None:
    """K7: горизонт двигается только вперёд, `max(старое, новое)`.

    Уборка с `descriptions=30` пишет горизонт `2026-07-02`; следующая
    уборка с более мягким `descriptions=365` целится в `2025-08-01` —
    дальше в прошлое. Без монотонности горизонт откатился бы назад и
    отменил собственное обещание: файл утверждал бы «за 2025-08-01
    описаний нет», хотя на самом деле их нет только за 2026-07-02, — и
    `report --since 200d` показал бы неполную выборку как полную,
    ничего не сказав об этом.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"

    execute(repo, reports, state, NOW, CleanupDays(descriptions=30))
    strict_horizon = horizon(state)
    assert strict_horizon == date(2026, 7, 2)

    execute(repo, reports, state, NOW, CleanupDays(descriptions=365))
    assert horizon(state) == strict_horizon  # не откатился на 2025-08-01


def test_a_future_horizon_on_disk_does_not_block_an_honest_write(tmp_path: Path) -> None:
    """Раунд починки 2 (ревью Task 4): горизонт из будущего не залипает навсегда.

    Горизонт по построению — это `now - неотрицательный срок`
    (`CleanupDays.__post_init__` отвергает отрицательный срок), то есть
    честно посчитанное значение не может быть позже `now`. Дата на
    диске, оказавшаяся ВПЕРЕДИ `now` (порча, ручная правка, баг), не
    может быть правдой ни при каком раскладе — но она СИНТАКСИЧЕСКИ
    валидна, и `horizon()` не перехватывает её как мусор. Починка I-4
    (`max(старое, новое)`) внесла регрессию: с такой датой на диске
    `max` всегда возвращает её и залипает навсегда — следующая честная
    запись никогда её не перекроет. Цена — не потеря данных, а потеря
    сигнала: `report --since` предупреждало бы ПОСТОЯННО, что и на любую
    границу, — то же обесценивание, что чинит R-3 для `partial`.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / HORIZON_FILE).write_text("2099-01-01\n", encoding="utf-8")

    execute(repo, reports, state, NOW, CleanupDays(descriptions=90))

    assert horizon(state) == date(2026, 5, 3)  # не застрял на 2099-01-01


def test_horizon_equal_to_today_is_trusted_and_not_rolled_back(tmp_path: Path) -> None:
    """Раунд починки 3 (ревью Task 4, Important-1): граница `existing == now.date()`.

    Собственное сомнение прошлого раунда оказалось верным: нестрогое
    сравнение (`<=`) выбрано правильно, но ни один тест не проверял
    именно РАВЕНСТВО — только заведомо будущую (`2099-01-01`) и заведомо
    прошлую (`test_horizon_never_moves_backward`) даты. Ревьюер проверил
    исполнением, что мутация `<=` → `<` на этой границе реально ломает
    монотонность: сохранённый горизонт, совпадающий с сегодняшним днём,
    обязан участвовать в `max`, а не отбрасываться как «будущий» и
    откатываться на более раннюю честную дату.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / HORIZON_FILE).write_text(f"{NOW.date():%Y-%m-%d}\n", encoding="utf-8")

    execute(repo, reports, state, NOW, CleanupDays(descriptions=90))

    assert horizon(state) == NOW.date()  # не откатился на 2026-05-03


def test_horizon_exactly_at_the_clock_skew_tolerance_edge_is_still_trusted(tmp_path: Path) -> None:
    """Раунд починки 3: сторож обязан целиться в ДЕЙСТВУЮЩУЮ границу кода.

    Допуск `HORIZON_CLOCK_SKEW_TOLERANCE`, добавленный этим же раундом
    (Important-2), сдвинул фактическую границу сравнения с `now.date()`
    на `now.date() + HORIZON_CLOCK_SKEW_TOLERANCE`. Соседний тест
    (`..._equal_to_today_...`) проверяет реалистичный, но уже не
    предельный случай — после появления допуска `existing == now.date()`
    лежит безопасно ВНУТРИ диапазона, а не на его краю, и мутация `<=` →
    `<` его не красит (проверено). Только сторож на равенство РОВНО
    краю допуска ловит эту мутацию — используется сама константа, а не
    захардкоженное число дней, поэтому тест не протухнет, если допуск
    когда-нибудь изменится.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    saved = NOW.date() + HORIZON_CLOCK_SKEW_TOLERANCE
    (state / HORIZON_FILE).write_text(f"{saved:%Y-%m-%d}\n", encoding="utf-8")

    execute(repo, reports, state, NOW, CleanupDays(descriptions=90))

    assert horizon(state) == saved  # не откатился на 2026-05-03


def test_a_small_clock_rollback_still_trusts_the_saved_horizon(tmp_path: Path) -> None:
    """Раунд починки 3 (ревью Task 4, Important-2): защита от будущего не
    имеет права ломать монотонность при обычном дрожании часов.

    `execute()` получает `now` аргументом, и ничто не гарантирует, что
    между вызовами он растёт (коррекция NTP, ручная правка времени
    хоста — тот же класс события, ради которого в `storage/run_log.py`
    уже живёт `CLOCK_SKEW_TOLERANCE`). Часы здесь откатились на 20 часов
    назад — сохранённый горизонт при этом выглядит «из будущего»
    относительно нового `now` ровно на календарные сутки, но такой
    откат — рядовое дрожание часов, а не порча диска, и допуск обязан
    его прощать: сохранённая дата остаётся в силе, а не заменяется более
    ранней честной датой.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    saved = date(2026, 8, 1)
    (state / HORIZON_FILE).write_text(f"{saved:%Y-%m-%d}\n", encoding="utf-8")
    rolled_back_now = datetime(2026, 7, 31, 14, 0, tzinfo=UTC)  # на 20 часов раньше saved-дня

    execute(repo, reports, state, rolled_back_now, CleanupDays(descriptions=30))

    assert horizon(state) == saved  # не откатился на 2026-07-01


# --- I-5: горизонт пишется ПОСЛЕ уборки, не до ------------------------------


def test_horizon_is_written_only_after_the_cleanup_it_promises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Мутация, которую этот тест обязан ловить: перенос записи горизонта в
    начало `execute()`. Отказ `forget_descriptions` — то, что уборка
    обещает горизонтом, — обязан остановить запись горизонта, иначе файл
    начнёт утверждать то, чего на самом деле не произошло.
    """
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    reports = tmp_path / "reports"
    reports.mkdir()
    state = tmp_path / "state"

    def boom(cutoff: datetime) -> int:
        raise RuntimeError("бум")

    monkeypatch.setattr(repo, "forget_descriptions", boom)

    with pytest.raises(RuntimeError, match="бум"):
        execute(repo, reports, state, NOW, CleanupDays(descriptions=90))

    assert horizon(state) is None


# --- M-1: комментарий обещал больше, чем делает регулярка ------------------


def test_a_human_file_that_happens_to_start_with_a_date_is_removed_too(tmp_path: Path) -> None:
    """M-1: правило — форма имени, а не авторство.

    Комментарий у `_REPORT_NAME_RE` раньше обещал, что уборка не трогает
    «любые файлы человека». Файл человека, чьё имя само подпадает под
    образец даты, удаляется наравне с отчётами — так вело себя и раньше
    (поведение не менялось), протухла только формулировка комментария.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    human_file = report_file(reports, date(2020, 1, 1), suffix="-мои-заметки.txt")
    db = tmp_path / "hh.db"
    repo = SqliteRepository(db)
    repo.init_schema()
    state = tmp_path / "state"

    execute(repo, reports, state, NOW, CleanupDays(reports=90))

    assert not human_file.exists()


# --- Отказ, названный словами в возвращённом значении -----------------------


def test_describe_names_the_errors_it_carries() -> None:
    """Общее требование раунда: отказ обязан быть виден в том, что
    возвращается наверх, а не только в логе — `describe()` печатает его
    для человека, а будущий CLI переиспользует этот текст."""
    result = CleanupPlan(
        descriptions=0,
        description_bytes=0,
        runs=0,
        report_files=0,
        report_bytes=0,
        descriptions_cutoff=NOW,
        errors=("каталог отчётов /data/reports недоступен: [Errno 13] Permission denied",),
    )
    assert "ОШИБКА" in result.describe(applied=True)
    assert "Permission denied" in result.describe(applied=True)
