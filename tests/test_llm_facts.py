"""Извлечение фактов из описания вакансии.

Роли разведены замером §0.3 спеки, а не вкусом: 8B-модель БЕРЁТ из
текста лучше, чем СУДИТ о нём. Поэтому здесь только то, что в тексте
написано, — стек, требуемый опыт, грейд, — и ни одного вопроса «подходит
ли».
"""

import json

import httpx
import pytest
import respx

from hh_search.config.models import LlmConfig
from hh_search.domain.models import VacancyFacts
from hh_search.llm.client import OllamaClient
from hh_search.llm.facts import FACTS_SCHEMA, extract_facts

BASE = "http://ollama.test:11434"


def make_client() -> OllamaClient:
    return OllamaClient(LlmConfig(base_url=BASE, chat_model="llama3", timeout_sec=5), base_url=BASE)


def answer(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": json.dumps(payload)}})


@respx.mock
def test_facts_are_parsed_from_the_models_answer() -> None:
    respx.post(f"{BASE}/api/chat").mock(
        return_value=answer(
            {"stack": ["Python", "FastApi", "Kafka"], "required_years": 3, "seniority": "senior"}
        )
    )

    facts = extract_facts(make_client(), "Python-разработчик", "нужен Python и Kafka")

    assert facts == VacancyFacts(
        stack=["Python", "FastApi", "Kafka"], required_years=3, seniority="senior"
    )


@respx.mock
def test_schema_is_sent_as_an_object_with_the_enum() -> None:
    """Замер §0.4: enum в схеме — единственное, что удерживает грейд.

    Со свободным `format: "json"` та же модель вернула `"middle+/senior"`.
    """
    route = respx.post(f"{BASE}/api/chat").mock(return_value=answer({"stack": []}))

    extract_facts(make_client(), "Заголовок", "описание")

    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == FACTS_SCHEMA
    assert sent["format"]["properties"]["seniority"]["enum"] == [
        "junior",
        "middle",
        "senior",
        "lead",
    ]


@respx.mock
def test_answer_outside_the_enum_costs_the_facts_and_not_the_run() -> None:
    """Схему принуждает Ollama — внешняя система, которая обновляется без нас.

    Валидация здесь поэтому не подстраховка, а граница: доверять чужому
    демону целостность своих данных нельзя. Цена промаха — `None` у одной
    вакансии, а не упавший прогон.
    """
    respx.post(f"{BASE}/api/chat").mock(
        return_value=answer({"stack": ["Python"], "seniority": "middle+/senior"})
    )

    assert extract_facts(make_client(), "Заголовок", "описание") is None


@respx.mock
def test_unreachable_model_costs_the_facts_and_not_the_run() -> None:
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    assert extract_facts(make_client(), "Заголовок", "описание") is None


@respx.mock
def test_absent_fields_are_none_and_not_invented() -> None:
    """Чего в тексте нет — того нет. Выдуманный опыт хуже неизвестного."""
    respx.post(f"{BASE}/api/chat").mock(return_value=answer({"stack": ["Python"]}))

    facts = extract_facts(make_client(), "Заголовок", "описание")

    assert facts is not None
    assert (facts.required_years, facts.seniority) == (None, None)


@pytest.mark.parametrize("years", [-1, 100])
def test_absurd_experience_is_refused(years: int) -> None:
    """Отрицательный и столетний опыт — галлюцинация, а не факт.

    Границы стоят в модели, а не в промпте: промпт — просьба, а модель
    отказывает. Верхняя взята с запасом к человеческой жизни, нижняя
    очевидна.
    """
    with pytest.raises(ValueError):
        VacancyFacts(required_years=years)


def test_stack_keeps_the_order_the_model_gave() -> None:
    """Порядок стека — не множество: модель называет главное первым.

    Замер §0.3: `Python, FastApi, SqlAlchemy, PostgreSQL, ...` — так они
    и стоят в тексте вакансии, от основного к вспомогательному.
    """
    facts = VacancyFacts(stack=["Python", "FastApi", "Kafka"])

    assert facts.stack == ["Python", "FastApi", "Kafka"]


@respx.mock
def test_an_extra_field_in_the_answer_is_refused_and_not_ignored() -> None:
    """Лишнее поле — признак, что схема и разбор разъехались. Молчать нельзя.

    Разъезжаются они не сами: это случается при правке схемы, при смене
    модели и при обновлении Ollama, то есть в моменты, когда никто не
    смотрит. Тихо отброшенное поле означало бы, что мы годами пишем в
    базу факты, которых модель уже не даёт, и узнаем об этом от глаза
    владельца. Проверено мутацией: без этого теста снятие `extra="forbid"`
    не красило ничего.
    """
    respx.post(f"{BASE}/api/chat").mock(
        return_value=answer({"stack": ["Python"], "summary": "команда делает платформу"})
    )

    assert extract_facts(make_client(), "Заголовок", "описание") is None
