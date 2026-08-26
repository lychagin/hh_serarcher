"""Извлечение фактов из описания вакансии локальной моделью.

Здесь нет ни одного вопроса «подходит ли». Роли разведены замером
2026-08-26 (§0.3 спеки `2026-08-26-local-llm-design.md`), а не вкусом:
8B-модель БЕРЁТ из текста заметно лучше, чем СУДИТ о нём. На трёх
релевантных вакансиях с ключевой оценкой 87.3 она выдала одну и ту же
константу 60 — и она же с той же вакансии точно выписала весь стек.
"""

import logging

from pydantic import ValidationError

from hh_search.domain.models import VacancyFacts
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
