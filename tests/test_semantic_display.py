"""Семантика видна владельцу — иначе он не сможет о ней судить.

§6 спеки `2026-08-26-local-llm-design.md` откладывает решение о большем
весе семантики до того, как её увидит глаз владельца. Отложить решение до
взгляда и не показать величину — значит не отложить, а отменить.
"""

import csv
from datetime import UTC, datetime
from pathlib import Path

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.sinks.csv_sink import COLUMNS, CsvSink
from hh_search.sinks.markdown_sink import MarkdownSink

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


def test_semantic_is_the_last_csv_column() -> None:
    """В хвост, а не в середину — инвариант этого приёмника.

    Колонка в середине сдвинула бы все поля после себя для `DictReader`,
    читающего файл дня по заголовку, написанному прошлой версией. Молча.
    """
    assert COLUMNS[-1] == "semantic"


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
