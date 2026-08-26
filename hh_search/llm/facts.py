"""Извлечение фактов из описания вакансии локальной моделью.

Здесь нет ни одного вопроса «подходит ли». Роли разведены замером
2026-08-26 (§0.3 спеки `2026-08-26-local-llm-design.md`), а не вкусом:
8B-модель БЕРЁТ из текста заметно лучше, чем СУДИТ о нём. На трёх
релевантных вакансиях с ключевой оценкой 87.3 она выдала одну и ту же
константу 60 — и она же с той же вакансии точно выписала весь стек.
"""

import logging

from pydantic import ValidationError

from hh_search.config.models import ProfileConfig
from hh_search.domain.models import Opinion, Relocation, VacancyFacts
from hh_search.errors import LlmUnavailable
from hh_search.llm.client import OllamaClient

logger = logging.getLogger(__name__)

# Схема передаётся ОБЪЕКТОМ и с `enum`, а не строкой `"json"`. Замер
# §0.4: со свободным форматом та же модель на том же входе вернула
# грейд `"middle+/senior"` — мимо перечисления, которое было написано в
# промпте словами. Промпт — просьба, схема — ограничение генерации.
FACTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "stack": {"type": "array", "items": {"type": "string"}},
        "required_years": {"type": ["integer", "null"]},
        "seniority": {
            "type": ["string", "null"],
            "enum": ["junior", "middle", "senior", "lead"],
        },
    },
    "required": ["stack"],
}

_SYSTEM = (
    "Извлеки факты из текста вакансии. Не рассуждай и не додумывай: пиши только то, "
    "что названо в тексте, чего нет — оставляй null. "
    "stack — технологии, названные в тексте, в порядке появления. "
    "required_years — требуемый опыт в годах числом. "
    "seniority — грейд одним из: junior, middle, senior, lead."
)


def extract_facts(client: OllamaClient, title: str, description: str) -> VacancyFacts | None:
    """Факты или `None`. `None` — штатный исход, а не авария.

    Отказ модели, ответ мимо схемы и лишнее поле дают один и тот же
    результат: у вакансии нет фактов, она уходит в отчёт без них. Это
    §4 спеки в применении к этому шагу — цена промаха ограничена одной
    вакансией, и ронять из-за неё прогон нельзя.

    Валидация обязательна, хотя схему принуждает Ollama. Принуждение
    живёт во ВНЕШНЕЙ системе, версия которой меняется без нашего ведома,
    а отвечать за целостность собственных данных доверием к чужому
    демону — тот самый тихий отказ, против которого написан весь слой
    хранилища.
    """
    try:
        answer = client.chat(_SYSTEM, f"{title}\n{description}", FACTS_SCHEMA)
    except LlmUnavailable as error:
        logger.debug("факты не извлечены (%s)", error)
        return None
    try:
        return VacancyFacts.model_validate(answer)
    except ValidationError as error:
        logger.warning(
            "модель ответила мимо схемы (%s): вакансия уйдёт в отчёт без фактов. Ответ: %.200s",
            error.errors()[0]["type"],
            answer,
        )
        return None


# Схема уточнения переезда. Класса «речь не о людях» в ней НЕТ: замер
# §0.7 показал, что модель не выбирает его никогда — включая обе живые
# вакансии, где переезжали приложения. Технический смысл отсеивает
# `filtering/relocation.py`, до вызова и без сети.
RELOCATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["required", "offered"]},
        "city": {"type": ["string", "null"]},
    },
    "required": ["kind"],
}

_RELOCATION_SYSTEM = (
    "В тексте вакансии есть речь о переезде сотрудника. Ответь на два вопроса. "
    "kind: 'required' — переехать надо, чтобы работать; 'offered' — переезд возможен "
    "по желанию или предлагается как льгота. "
    "city — город или страна переезда, если названы в тексте, иначе null. Не додумывай."
)


def extract_relocation(client: OllamaClient, title: str, description: str) -> Relocation | None:
    """Город и вид переезда — или `None`.

    Зовётся ТОЛЬКО у вакансий, где переезд уже найден текстом
    (`filtering.relocation.mentions_relocation`), то есть примерно у
    шести процентов: замер 2026-08-26 — девять вакансий из ста
    пятидесяти, пятнадцать секунд на прогон вместо пяти минут.

    Вопроса «есть ли переезд» модели не задаётся вовсе: на нём она нашла
    пять упоминаний из одиннадцати. Её отказ стоит городу и виду, но не
    самой пометке — ту поставили без сети.
    """
    try:
        answer = client.chat(_RELOCATION_SYSTEM, f"{title}\n{description}", RELOCATION_SCHEMA)
    except LlmUnavailable as error:
        logger.debug("переезд не уточнён (%s)", error)
        return None
    try:
        return Relocation.model_validate(answer)
    except ValidationError:
        logger.warning("модель ответила мимо схемы переезда: %.200s", answer)
        return None


# Схема мнения. Фактов в ней НЕТ, и это замер, а не вкус: §0.9 спеки —
# тот же вопрос, заданный одним запросом вместе с извлечением, обрушивает
# оба сигнала. Вакансия «Technical Team Lead (C# / Python)» получала 35
# отдельным вопросом и 75 совмещённым, то есть ровно то расхождение с
# ключевой оценкой, ради которого мнение и заводится, исчезало.
OPINION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


def _profile_line(profile: ProfileConfig) -> str:
    """Профиль для промпта — из `profile.yaml`, а не константой в коде.

    Иначе правка сигналов владельцем меняла бы ключевую оценку и не
    меняла бы мнение модели: два ответа на один вопрос, расходящиеся
    молча и тем сильнее, чем дольше живёт проект.
    """
    signals = profile.signals
    groups = (signals.title_roles, signals.title_tech, signals.stack, signals.domain)
    words = [spelling for group in groups for entry in group for spelling in entry]
    return ", ".join(words)


def extract_opinion(
    client: OllamaClient, profile: ProfileConfig, title: str, description: str
) -> Opinion | None:
    """Оценка модели и одна строка почему — или `None`.

    Зовётся только у вакансий выше порога отчёта: мнение показывается
    там, где владелец его читает, и платить за него на всём корпусе
    незачем. Замер 2026-08-26 — 34 вакансии из 573, около минуты.
    """
    system = (
        "Оцени, насколько вакансия подходит кандидату, и верни СТРОГО JSON. "
        "score — от 0 до 100. reason — одно предложение по-русски, почему. "
        f"Профиль кандидата: {_profile_line(profile)}."
    )
    try:
        answer = client.chat(system, f"{title}\n{description}", OPINION_SCHEMA)
    except LlmUnavailable as error:
        logger.debug("мнение не получено (%s)", error)
        return None
    try:
        return Opinion.model_validate(answer)
    except ValidationError:
        logger.warning("модель ответила мимо схемы мнения: %.200s", answer)
        return None
