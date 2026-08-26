"""Контрактные тесты к ЖИВОЙ Ollama. В ворота не входят.

Запуск вручную: `uv run pytest -m llm`. До того как открыт доступ
(§2 спеки `docs/superpowers/specs/2026-08-26-local-llm-design.md`), они
пропускаются с внятной причиной, а не падают: недостижимая модель — это
состояние машины, а не дефект кода.

Проверяют ровно то, что нельзя проверить заглушкой: поведение самой
Ollama. Всё, что можно, проверено на `respx` в `test_llm_client.py`.
"""

import pytest

from hh_search.config.models import LlmConfig
from hh_search.errors import LlmUnavailable
from hh_search.llm.client import OllamaClient, resolve_base_url

pytestmark = pytest.mark.llm

GRADE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"seniority": {"type": "string", "enum": ["junior", "middle", "senior", "lead"]}},
    "required": ["seniority"],
}


@pytest.fixture()
def live() -> OllamaClient:
    config = LlmConfig(semantic=True)
    client = OllamaClient(config, resolve_base_url(config.base_url))
    try:
        client.embed(["проверка доступности"])
    except LlmUnavailable as error:
        client.close()
        pytest.skip(f"живая Ollama недоступна: {error}")
    return client


def test_embedding_has_the_dimension_the_spec_names(live: OllamaClient) -> None:
    """1024 — число из §5 спеки, на нём стоит расчёт 4 КБ на вакансию."""
    with live:
        assert len(live.embed(["Ведущий разработчик C++"])[0]) == 1024


def test_a_vacancy_is_closer_to_the_profile_than_an_unrelated_one(live: OllamaClient) -> None:
    """Замер §0.2 в форме теста: модель отличает своё от чужого.

    Числа не закрепляются — они поплывут от смены версии модели, и
    сторож с числом протух бы сам. Закрепляется ЗНАК разницы, на котором
    и стоит решение: близкая вакансия ближе далёкой.
    """
    from hh_search.llm.semantic import cosine

    with live:
        profile, close, far = live.embed(
            [
                "Team Lead, C++, Embedded Linux, Yocto, ARM, телеком, встраиваемые системы",
                "Ведущий инженер-программист C++ для встраиваемых систем на Linux, Yocto, ARM",
                "Администратор учебного процесса: расписание, документооборот, работа с группами",
            ]
        )

    assert cosine(profile, close) > cosine(profile, far)


def test_schema_with_an_enum_is_actually_enforced(live: OllamaClient) -> None:
    """Замер §0.4: со схемой-ОБЪЕКТОМ модель не выходит за перечисление.

    Именно это утверждение делает валидацию на выходе достаточной, а не
    отчаянной, и именно оно способно тихо перестать быть верным при смене
    версии Ollama — то есть проверяться обязано живьём.
    """
    with live:
        answer = live.chat(
            "Определи грейд вакансии. Верни только JSON.",
            "Python-разработчик middle+/senior, требуется опыт от 3 лет",
            GRADE_SCHEMA,
        )

    assert answer["seniority"] in {"junior", "middle", "senior", "lead"}
