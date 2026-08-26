"""Порядок вакансий в отчёте: семантика разрывает связки и только их."""

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.sinks.ordering import by_relevance


def make(vacancy_id: str, total: float, semantic: float | None = None) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title=f"Вакансия {vacancy_id}",
            found_by_query="programmist",
        ),
        details=VacancyDetails(description="описание"),
        score=ScoreBreakdown(
            title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=total
        ),
        cluster="backend",
        semantic=semantic,
    )


def ids(vacancies: list[ScoredVacancy]) -> list[str]:
    return [item.discovered.id for item in vacancies]


def test_higher_keyword_score_wins_regardless_of_semantics() -> None:
    """Семантика НЕ обгоняет ключевую оценку — это и есть её безопасность.

    Замер §0.2.1 спеки: у 246 из 539 отсеянных вакансий косинус выше, чем
    у худшей прошедшей порог. Дай ему обгонять оценку — и он потянул бы
    наверх почти половину отсева.
    """
    ordered = by_relevance(
        [make("низкая", 60.0, semantic=0.99), make("высокая", 87.3, semantic=0.10)]
    )

    assert ids(ordered) == ["высокая", "низкая"]


def test_semantics_breaks_a_tie_of_equal_keyword_scores() -> None:
    """Связка — 55% вакансий выше порога (замер по живой базе 2026-08-26)."""
    ordered = by_relevance(
        [make("ближе-по-смыслу", 87.3, semantic=0.669), make("дальше", 87.3, semantic=0.601)]
    )

    assert ids(ordered) == ["ближе-по-смыслу", "дальше"]


def test_without_any_semantics_the_order_is_the_incoming_one() -> None:
    """Модель недоступна — порядок обязан быть В ТОЧНОСТИ прежним.

    Это наблюдаемая форма центрального инварианта §4 спеки: выключенный
    Windows не имеет права переставлять отчёт. Сортировка поэтому
    устойчивая, а `None` не превращается в ноль.
    """
    incoming = [make("первая", 87.3), make("вторая", 87.3), make("третья", 87.3)]

    assert ids(by_relevance(incoming)) == ["первая", "вторая", "третья"]


def test_vacancy_without_a_vector_goes_last_within_its_tie_only() -> None:
    """«Не считалось» уступает посчитанному — но только внутри своей связки.

    Ноль вместо `None` был бы хуже: он утверждал бы, что вакансия далека
    от профиля, тогда как о ней просто ничего не известно. Внутри связки
    кто-то обязан быть последним, и им становится тот, о ком нет данных;
    за пределы связки это не выходит.
    """
    ordered = by_relevance(
        [
            make("без-вектора", 87.3),
            make("с-вектором", 87.3, semantic=0.01),
            make("ниже", 80.0, semantic=0.99),
        ]
    )

    assert ids(ordered) == ["с-вектором", "без-вектора", "ниже"]
