"""Уборка: план, исполнение и то, чего она не трогает никогда."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hh_search.domain.models import DiscoveredVacancy, Salary, ScoreBreakdown, VacancyDetails
from hh_search.pipeline.cleanup import (
    PROTECTED_DAYS,
    CleanupDays,
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

    `now` теста (2030-06-15) заведомо не совпадает с реальной датой машины.
    Файл датирован РЕАЛЬНЫМ сегодня — если бы граница защищённого окна
    считалась от `date.today()`, а не от переданного `now`, такой файл
    остался бы защищён на годы дольше, чем обещает контракт, и тест,
    зафиксировавший обе даты числом, был бы зелён сегодня и красен через
    сутки. Здесь `date.today()` взят из настоящих часов исполнения, а не
    захардкожен, поэтому сторож остаётся honest на любую дату запуска.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    future_now = datetime(2030, 6, 15, 10, 0, tzinfo=UTC)
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
