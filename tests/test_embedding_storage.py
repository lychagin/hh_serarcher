"""Хранение вектора: запись, очередь, чтение, обесценивание сменой модели."""

import pytest

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails
from hh_search.llm.semantic import pack_vector
from hh_search.storage.repository import SqliteRepository

MODEL = "bge-m3"
OTHER_MODEL = "nomic-embed-text"


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def enriched(repo: SqliteRepository, vacancy_id: str = "1", description: str = "Yocto BSP") -> None:
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
        VacancyDetails(description=description),
        ScoreBreakdown(title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=87.3),
    )


def test_saved_vector_is_read_back_for_its_model(repo: SqliteRepository) -> None:
    enriched(repo)

    repo.save_embedding("1", MODEL, pack_vector([0.5, -0.25]))

    assert repo.embeddings(["1"], MODEL) == {"1": pack_vector([0.5, -0.25])}


def test_vector_of_another_model_is_not_returned(repo: SqliteRepository) -> None:
    """Смена модели обязана обесценивать векторы САМА, без чистки базы.

    Вектор bge-m3 и вектор любой другой модели живут в разных
    пространствах: косинус между ними посчитается, выйдет правдоподобное
    число и не будет значить ничего. §5 спеки
    `2026-08-26-local-llm-design.md`.
    """
    enriched(repo)
    repo.save_embedding("1", OTHER_MODEL, pack_vector([0.5, -0.25]))

    assert repo.embeddings(["1"], MODEL) == {}


def test_vacancy_without_a_vector_is_offered_for_embedding(repo: SqliteRepository) -> None:
    enriched(repo, description="Yocto BSP ARM")

    pending = repo.pending_embedding(MODEL, limit=10)

    assert [vacancy_id for vacancy_id, _ in pending] == ["1"]
    text = pending[0][1]
    assert "Ведущий разработчик" in text and "Yocto BSP ARM" in text


def test_vacancy_with_a_current_vector_is_not_offered_again(repo: SqliteRepository) -> None:
    enriched(repo)
    repo.save_embedding("1", MODEL, pack_vector([0.5, -0.25]))

    assert repo.pending_embedding(MODEL, limit=10) == []


def test_vacancy_embedded_by_another_model_is_offered_again(repo: SqliteRepository) -> None:
    """Правка `llm.embed_model` обязана ставить корпус в очередь заново.

    Иначе смена модели дала бы базу из двух несравнимых половин, каждая
    из которых по отдельности выглядит здоровой.
    """
    enriched(repo)
    repo.save_embedding("1", OTHER_MODEL, pack_vector([0.5, -0.25]))

    assert [vacancy_id for vacancy_id, _ in repo.pending_embedding(MODEL, limit=10)] == ["1"]


def test_vacancy_without_a_description_is_not_offered(repo: SqliteRepository) -> None:
    """Эмбеддить нечего: описание либо ещё не скачано, либо снято уборкой."""
    repo.add_discovered(
        DiscoveredVacancy(
            id="2", url="https://hh.ru/vacancy/2", title="Ведущий", found_by_query="programmist"
        ),
        cluster="backend",
        weight=8,
    )

    assert repo.pending_embedding(MODEL, limit=10) == []


def test_pending_embedding_honours_the_limit(repo: SqliteRepository) -> None:
    for number in range(5):
        enriched(repo, vacancy_id=str(number))

    assert len(repo.pending_embedding(MODEL, limit=2)) == 2


def test_forgetting_descriptions_drops_the_vector_with_them(repo: SqliteRepository) -> None:
    """Вектор — производная описания и переживать его не имеет права.

    Уборка обнуляет описание отправленных вакансий старше порога (спека
    2026-08-01 §3). Вектор, оставшийся без исходника, — данные, которые
    нечем перепроверить: ни пересчитать, ни объяснить.
    """
    from datetime import UTC, datetime, timedelta

    enriched(repo)
    repo.save_embedding("1", MODEL, pack_vector([0.5, -0.25]))
    repo.mark_reported(["1"])

    repo.forget_descriptions(datetime.now(UTC) + timedelta(days=1))

    assert repo.embeddings(["1"], MODEL) == {}


def test_empty_id_list_gives_an_empty_result(repo: SqliteRepository) -> None:
    """Пустой список на входе — пустой словарь на выходе, без исключения.

    Отчёт с пустой очередью зовёт чтение векторов со списком нулевой
    длины, и это штатный путь, а не край. Ранний возврат отсюда УБРАН:
    измерено, что SQLite принимает `id IN ()` и отдаёт пусто, то есть
    ветка не меняла наблюдаемого поведения и мутацией не ловилась.
    Утверждение теста при этом остаётся контрактом реализации — в том
    числе для `PostgresRepository`, обещанного §4.2 спеки 2026-07-27,
    где `IN ()` уже синтаксическая ошибка.
    """
    assert repo.embeddings([], MODEL) == {}
