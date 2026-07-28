from typing import Protocol

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails


class Scorer(Protocol):
    """Точка расширения: сюда позже встанет оценщик на LLM (Claude, OpenAI, локальная модель)."""

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown: ...
