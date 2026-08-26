"""Семантика видна владельцу — иначе он не сможет о ней судить.

§6 спеки `2026-08-26-local-llm-design.md` откладывает решение о большем
весе семантики до того, как её увидит глаз владельца. Отложить решение до
взгляда и не показать величину — значит не отложить, а отменить.
"""

import csv
from datetime import UTC, datetime
from pathlib import Path

from hh_search.domain.models import (
    DiscoveredVacancy,
    Opinion,
    Relocation,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
    VacancyFacts,
)
from hh_search.sinks.csv_sink import COLUMNS, CsvSink
from hh_search.sinks.markdown_sink import MarkdownSink
from hh_search.sinks.ordering import by_relevance

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def make(vacancy_id: str, total: float, semantic: float | None) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title=f"Ведущий разработчик {vacancy_id}",
            company="Контора",
            found_by_query="programmist",
        ),
        details=VacancyDetails(description="Yocto BSP ARM " * 20),
        score=ScoreBreakdown(
            title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=total
        ),
        cluster="backend",
        semantic=semantic,
    )


def test_new_columns_are_appended_and_never_inserted() -> None:
    """Прежние колонки стоят на прежних местах — вот настоящий инвариант.

    Колонка, вставленная в СЕРЕДИНУ, сдвинула бы все поля после себя для
    `DictReader`, читающего файл дня по заголовку, написанному прошлой
    версией: `salary_from` получил бы значение формата, `url` потерял бы
    последнее поле — молча, без исключения.

    Сторожится префикс, а не «последняя колонка называется так-то»:
    прежняя редакция этого теста утверждала второе и покраснела от
    добавления следующей же колонки, ничего при этом не защитив.
    """
    established = [
        "id",
        "score",
        "cluster",
        "title",
        "company",
        "area",
        "salary_from",
        "salary_to",
        "currency",
        "published_at",
        "listing",
        "url",
        "work_formats",
    ]

    assert COLUMNS[: len(established)] == established


def test_csv_carries_the_value_and_leaves_it_empty_when_unknown(tmp_path: Path) -> None:
    """Пустая ячейка, а не ноль: «не считалось» — не «далеко от профиля»."""
    sink = CsvSink(tmp_path)

    sink.emit([make("1", 87.3, 0.669), make("2", 87.3, None)], NOW)

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        rows = {row["id"]: row["semantic"] for row in csv.DictReader(handle, delimiter=";")}
    assert rows["1"] == "0.669"
    assert rows["2"] == ""


def test_markdown_top_entry_shows_the_value_next_to_the_score(tmp_path: Path) -> None:
    sink = MarkdownSink(tmp_path, threshold=60.0)

    sink.emit([make("1", 87.3, 0.669)], NOW)

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "87.3" in text
    assert "0.669" in text


def test_markdown_without_semantics_looks_exactly_as_before(tmp_path: Path) -> None:
    """Прогон без модели даёт отчёт прежнего вида — до знака.

    Наблюдаемая форма §4: выключенный Windows не имеет права менять то,
    что владелец читает каждое утро.
    """
    sink = MarkdownSink(tmp_path, threshold=60.0)

    sink.emit([make("1", 87.3, None)], NOW)

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "**[Ведущий разработчик 1](https://hh.ru/vacancy/1)** — 87.3\n" in text


# --- Факты ----------------------------------------------------------------


def with_facts(vacancy_id: str, facts: VacancyFacts | None) -> ScoredVacancy:
    return make(vacancy_id, 87.3, 0.669).model_copy(update={"facts": facts})


def test_stack_column_lists_what_the_model_found(tmp_path: Path) -> None:
    """Стек в CSV — не дубликат скоринга.

    `KeywordScorer` находит только сигналы из `profile.yaml`; модель
    выписывает то, что в тексте НАЗВАНО, включая технологии, которых
    владелец не искал. Это и есть новое знание, ради которого шаг стоит
    пяти минут прогона.
    """
    CsvSink(tmp_path).emit(
        [with_facts("1", VacancyFacts(stack=["Python", "Kafka"], seniority="senior"))], NOW
    )

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert row["stack"] == "Python, Kafka"
    assert row["seniority"] == "senior"


def test_csv_leaves_fact_columns_empty_when_nothing_was_extracted(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit([with_facts("1", None)], NOW)

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert (row["stack"], row["seniority"]) == ("", "")


def test_markdown_shows_the_extracted_stack(tmp_path: Path) -> None:
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [with_facts("1", VacancyFacts(stack=["Python", "Kafka"], required_years=3))], NOW
    )

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "Python, Kafka" in text
    assert "3" in text


def test_markdown_without_facts_adds_no_empty_line(tmp_path: Path) -> None:
    """Пустых «стек: » в отчёте быть не должно — их читают глазами."""
    MarkdownSink(tmp_path, threshold=60.0).emit([with_facts("1", None)], NOW)

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "стек" not in text.lower()


# --- Переезд --------------------------------------------------------------


def test_relocation_is_the_first_thing_said_about_a_vacancy(tmp_path: Path) -> None:
    """Переезд идёт первым в строке фактов, раньше стека и грейда.

    Это единственная её часть, способная закрыть вакансию для владельца
    целиком: стек можно доучить, грейд обсудить, а переезд в Елабугу — это
    решение о жизни, а не о работе.
    """
    facts = VacancyFacts(
        stack=["Python"],
        seniority="lead",
        relocation=Relocation(kind="required", city="Елабуга"),
    )
    MarkdownSink(tmp_path, threshold=60.0).emit([with_facts("1", facts)], NOW)

    line = next(
        row
        for row in (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8").splitlines()
        if "переезд" in row
    )
    assert line.index("переезд") < line.index("стек")
    assert "Елабуга" in line and "требуется" in line


def test_optional_relocation_is_not_called_required(tmp_path: Path) -> None:
    """«Возможна релокация на Кипр по желанию» — не то же, что «работа в Елабуге».

    Разницу называет модель, и напечатать одно вместо другого значило бы
    отпугнуть владельца от удалённой вакансии с приятной льготой.
    """
    facts = VacancyFacts(relocation=Relocation(kind="offered", city="Кипр"))
    MarkdownSink(tmp_path, threshold=60.0).emit([with_facts("1", facts)], NOW)

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "по желанию" in text
    assert "требуется" not in text


def test_csv_carries_relocation_as_one_cell(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit(
        [with_facts("1", VacancyFacts(relocation=Relocation(kind="required", city="Елабуга")))],
        NOW,
    )

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert row["relocation"] == "требуется: Елабуга"


def test_csv_relocation_is_empty_without_one(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit([with_facts("1", VacancyFacts(stack=["Python"]))], NOW)

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert row["relocation"] == ""


def test_csv_calls_an_optional_relocation_optional(tmp_path: Path) -> None:
    """Проверено мутацией: без этого случая CSV мог называть льготу требованием.

    Соседний тест подаёт только `required`, и подмена вида на константу
    его не красила — то есть ошибка, отпугивающая владельца от удалённой
    вакансии с приятной льготой, прошла бы незамеченной.
    """
    CsvSink(tmp_path).emit(
        [with_facts("1", VacancyFacts(relocation=Relocation(kind="offered", city="Кипр")))], NOW
    )

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert row["relocation"] == "по желанию: Кипр"


# --- Мнение модели --------------------------------------------------------


def test_opinion_is_shown_with_its_reason(tmp_path: Path) -> None:
    """Оценка модели И причина — вместе, иначе число нечем поверить.

    Владелец решил (2026-08-26) показывать мнение, а не отсеивать по нему.
    Голая цифра «35» рядом с ключевой «94» ставит вопрос и не отвечает на
    него; «стек не соответствует профилю» отвечает.
    """
    facts = VacancyFacts(opinion=Opinion(score=35, reason="стек не соответствует профилю"))
    MarkdownSink(tmp_path, threshold=60.0).emit([with_facts("1", facts)], NOW)

    text = (tmp_path / f"{NOW:%Y-%m-%d}-new.md").read_text(encoding="utf-8")
    assert "35" in text
    assert "стек не соответствует профилю" in text


def test_opinion_does_not_change_the_order() -> None:
    """Решение владельца: мнение ПОКАЗЫВАЕТСЯ, но ничего не двигает.

    Замер §0.8 показал, что расхождения модели с ключевой оценкой не
    случайны, и НЕ показал, что они верны. До того как владелец рассудит
    это на живых данных, дать мнению двигать отчёт значило бы принять
    решение за него — и тем лишить смысла сам показ.
    """
    # Баллы РАВНЫ, а семантика и мнение спорят: семантика ставит первым
    # «ближе-по-смыслу», мнение — «нравится-модели». Разный первый ключ
    # эту пару не различил бы вовсе (проверено мутацией: тест с разными
    # баллами оставался зелёным, когда мнение подменяло семантику в
    # сортировке, — решал уже первый ключ).
    closer_but_disliked = make("ближе-по-смыслу", 87.3, semantic=0.9).model_copy(
        update={"facts": VacancyFacts(opinion=Opinion(score=10, reason="чужой стек"))}
    )
    farther_but_liked = make("нравится-модели", 87.3, semantic=0.1).model_copy(
        update={"facts": VacancyFacts(opinion=Opinion(score=95, reason="точное попадание"))}
    )

    ordered = by_relevance([farther_but_liked, closer_but_disliked])

    assert [item.discovered.id for item in ordered] == ["ближе-по-смыслу", "нравится-модели"]


def test_csv_carries_opinion_score_and_reason(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit(
        [with_facts("1", VacancyFacts(opinion=Opinion(score=35, reason="чужой стек")))], NOW
    )

    with (tmp_path / f"{NOW:%Y-%m-%d}-new.csv").open(encoding="utf-8-sig") as handle:
        row = next(iter(csv.DictReader(handle, delimiter=";")))
    assert row["llm_score"] == "35"
    assert row["llm_reason"] == "чужой стек"
