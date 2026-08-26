"""Хранение фактов: запись, очередь, чтение, обесценивание сменой модели."""

import sqlite3
from pathlib import Path

import pytest

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails, VacancyFacts
from hh_search.storage.repository import SqliteRepository

MODEL = "llama3"
OTHER = "qwen2.5"
FACTS = VacancyFacts(stack=["Python", "Kafka"], required_years=3, seniority="senior")


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def enriched(repo: SqliteRepository, vacancy_id: str = "1") -> None:
    repo.add_discovered(
        DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title="Ведущий разработчик",
            found_by_query="programmist",
        ),
        cluster="backend",
        weight=8,
    )
    repo.save_enriched(
        vacancy_id,
        VacancyDetails(description="Yocto BSP ARM"),
        ScoreBreakdown(title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=87.3),
    )


def test_saved_facts_are_read_back(repo: SqliteRepository) -> None:
    enriched(repo)

    repo.save_facts("1", MODEL, FACTS)

    assert repo.facts(["1"], MODEL) == {"1": FACTS}


def test_facts_of_another_model_are_not_returned(repo: SqliteRepository) -> None:
    """Смена `llm.chat_model` обесценивает факты сама.

    Другая модель извлекает иначе — вплоть до другой шкалы грейда, — и
    отчёт, смешавший два поколения, выглядел бы согласованным, не будучи им.
    """
    enriched(repo)
    repo.save_facts("1", OTHER, FACTS)

    assert repo.facts(["1"], MODEL) == {}


def test_vacancy_without_facts_is_offered_for_extraction(repo: SqliteRepository) -> None:
    enriched(repo)

    pending = repo.pending_facts(MODEL, limit=10)

    assert [vacancy_id for vacancy_id, _, _ in pending] == ["1"]
    assert pending[0][1] == "Ведущий разработчик"
    assert pending[0][2] == "Yocto BSP ARM"


def test_vacancy_with_current_facts_is_not_offered_again(repo: SqliteRepository) -> None:
    enriched(repo)
    repo.save_facts("1", MODEL, FACTS)

    assert repo.pending_facts(MODEL, limit=10) == []


def test_unreadable_facts_cost_the_vacancy_its_facts_and_nothing_more(tmp_path: Path) -> None:
    """Испорченный JSON в колонке — не повод ронять чтение всего отчёта.

    Отдельный от `score_detail` случай и потому отдельное решение: за
    оценкой стоит карантин, потому что без неё вакансия не отправляется
    вовсе. Без фактов — отправляется, просто без них.

    База файловая, а порча вносится СЫРЫМ соединением — тем же приёмом,
    что и в `tests/test_repository.py`: испортить данные через сам
    репозиторий нельзя, он для того и написан.
    """
    path = tmp_path / "hh.db"
    with SqliteRepository(path) as repository:
        repository.init_schema()
        enriched(repository)
        repository.save_facts("1", MODEL, FACTS)
    raw = sqlite3.connect(path)
    raw.execute("UPDATE vacancy SET llm_facts = ? WHERE id = ?", ("{битый", "1"))
    raw.commit()
    raw.close()

    with SqliteRepository(path) as repository:
        assert repository.facts(["1"], MODEL) == {}


def test_forgetting_descriptions_drops_the_facts_with_them(repo: SqliteRepository) -> None:
    """Факты — производная описания, как и вектор."""
    from datetime import UTC, datetime, timedelta

    enriched(repo)
    repo.save_facts("1", MODEL, FACTS)
    repo.mark_reported(["1"])

    repo.forget_descriptions(datetime.now(UTC) + timedelta(days=1))

    assert repo.facts(["1"], MODEL) == {}
