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
from hh_search.domain.models import Opinion, Relocation, VacancyFacts
from hh_search.llm.client import OllamaClient
from hh_search.llm.facts import (
    FACTS_SCHEMA,
    OPINION_SCHEMA,
    RELOCATION_SCHEMA,
    extract_facts,
    extract_opinion,
    extract_relocation,
)
from tests.test_llm_semantic import PROFILE

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


# --- Переезд ---------------------------------------------------------------


@respx.mock
def test_relocation_is_asked_separately_and_only_about_the_city_and_kind() -> None:
    """Отдельный вопрос, а не поле в общей схеме, — решение замера.

    llama3, спрошенная «есть ли переезд», нашла пять упоминаний из
    одиннадцати. Слова находят все одиннадцать, поэтому НАХОДИТ регулярка
    (`filtering/relocation.py`), а модель отвечает только на то, в чём
    измеренно сильна: как называется город и требование это или льгота.
    Вопрос «есть ли переезд» ей больше не задаётся вовсе.
    """
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=answer({"kind": "required", "city": "Елабуга"})
    )

    relocation = extract_relocation(
        make_client(), "Инженер", "работа в Елабуге, поможем с переездом"
    )

    assert relocation == Relocation(kind="required", city="Елабуга")
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"]["properties"]["kind"]["enum"] == ["required", "offered"]


@respx.mock
def test_relocation_schema_has_no_not_people_option() -> None:
    """Класса «речь не о людях» в схеме НЕТ, и это тоже замер.

    Спрошенная про него llama3 не выбрала его ни разу из одиннадцати —
    включая обе вакансии, где переезжали приложения. Оставить в схеме
    вариант, который модель не выбирает никогда, значит создать видимость
    проверки: она отвечала бы `required` на «переезд приложений в
    Kubernetes» ровно так же, как отвечает сейчас. Технический смысл
    отсеян ДО вызова, детерминированно.
    """
    respx.post(f"{BASE}/api/chat").mock(return_value=answer({"kind": "offered"}))

    extract_relocation(make_client(), "Инженер", "возможна релокация на Кипр")

    assert "not_people" not in json.dumps(FACTS_SCHEMA) + json.dumps(RELOCATION_SCHEMA)


@respx.mock
def test_unknown_city_is_none_and_not_invented() -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=answer({"kind": "offered", "city": None}))

    relocation = extract_relocation(make_client(), "Инженер", "возможна релокация")

    assert relocation is not None
    assert relocation.city is None


@respx.mock
def test_unreachable_model_costs_the_relocation_detail_and_not_the_flag() -> None:
    """Модель молчит — пометка о переезде всё равно остаётся.

    Её ставит регулярка, без сети. Модель добавляет к пометке город и
    вид, и её отказ обязан стоить ровно этой прибавки (§4 спеки).
    """
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    assert extract_relocation(make_client(), "Инженер", "поможем с переездом") is None


# --- Мнение модели ---------------------------------------------------------


@respx.mock
def test_opinion_is_asked_by_its_own_request() -> None:
    """Отдельный запрос, а не поле в схеме фактов, — решение замера.

    Замер 2026-08-26: тот же вопрос, заданный ОДНИМ запросом вместе с
    фактами, обрушивает оба сигнала. Вакансия «Technical Team Lead
    (C# / Python)» получала 35 отдельным вопросом и 75 совмещённым —
    то есть ровно то расхождение с ключевой оценкой, ради которого
    мнение и заводится, исчезало. Стек при этом беднел:
    `Node.js, Nest, Redis, Docker, Jest, Kafka` превращалось в
    `Node.js, Nest.js, TypeScript, Git`.
    """
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=answer({"score": 35, "reason": "стек не соответствует профилю"})
    )

    opinion = extract_opinion(make_client(), PROFILE, "Team Lead C#", "нужен C# и .NET")

    assert opinion == Opinion(score=35, reason="стек не соответствует профилю")
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == OPINION_SCHEMA
    assert "stack" not in sent["format"]["properties"]


@respx.mock
def test_opinion_prompt_carries_the_profile() -> None:
    """Профиль берётся из `profile.yaml`, а не зашит в промпт константой.

    Иначе правка сигналов владельцем меняла бы ключевую оценку и НЕ
    меняла бы мнение модели — два ответа на один вопрос, расходящиеся
    молча и тем сильнее, чем дольше живёт проект.
    """
    route = respx.post(f"{BASE}/api/chat").mock(return_value=answer({"score": 50, "reason": "—"}))

    extract_opinion(make_client(), PROFILE, "Заголовок", "описание")

    system = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "yocto" in system.lower()
    assert "телеком" in system.lower()


@respx.mock
def test_score_outside_the_range_is_refused() -> None:
    """0..100 сторожится валидацией, а не только схемой.

    Ограничение живёт в Ollama, версия которой меняется без нас, а
    оценка уезжает в отчёт как число рядом с ключевой — и 850 там
    выглядело бы как факт.
    """
    respx.post(f"{BASE}/api/chat").mock(return_value=answer({"score": 850, "reason": "—"}))

    assert extract_opinion(make_client(), PROFILE, "Заголовок", "описание") is None


@respx.mock
def test_unreachable_model_costs_the_opinion_and_not_the_run() -> None:
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    assert extract_opinion(make_client(), PROFILE, "Заголовок", "описание") is None
