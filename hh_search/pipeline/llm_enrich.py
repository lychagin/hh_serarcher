"""Шаг конвейера: вектор описания и семантика в отчёте.

Стоит между обогащением и оценкой: описание уже скачано, оценка ещё не
посчитана. Весь модуль подчинён одному правилу — §4 спеки
`docs/superpowers/specs/2026-08-26-local-llm-design.md`: недоступная,
медленная или врущая модель не роняет прогон и не меняет ни одного
вердикта. Поэтому здесь нет ни одного пути, по которому `LlmUnavailable`
уходил бы наружу.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from hh_search.config.models import ProfileConfig
from hh_search.domain.models import ScoredVacancy
from hh_search.errors import LlmUnavailable
from hh_search.llm.client import OllamaClient
from hh_search.llm.semantic import cosine, pack_vector, profile_text, unpack_vector
from hh_search.storage.base import Repository

logger = logging.getLogger(__name__)

# Сколько описаний уезжает в один POST. Описание доходит до 4 КБ, и
# очередь в двести штук (умолчание `limits.llm_per_run`) одним телом дала
# бы запрос под мегабайт, который целиком пропадает от одного таймаута.
# Двадцать — размер, на котором снят замер §0.5 спеки.
_BATCH = 20


def embed_pending(client: OllamaClient, repo: Repository, model: str, limit: int) -> int:
    """Посчитать и записать векторы очереди. Возвращает, сколько записано.

    Отказ модели обрывает работу и возвращает уже записанное, а НЕ
    откатывает его: каждая пачка коммитится своими `save_embedding`, и
    выключившийся посреди прогона Ollama оставляет корпус наполовину
    посчитанным — что верно, потому что следующий прогон доберёт остаток
    той же выборкой.
    """
    pending = repo.pending_embedding(model, limit)
    if not pending:
        return 0
    written = 0
    for start in range(0, len(pending), _BATCH):
        batch = pending[start : start + _BATCH]
        try:
            vectors = client.embed([text for _, text in batch])
        except LlmUnavailable as error:
            # ОДНА строка за прогон, а не строка на вакансию: лог —
            # единственный канал наблюдаемости этого сервиса, и двести
            # сообщений о выключенном localhost утопили бы в себе
            # настоящую находку. Выход из цикла здесь же: если модель
            # недоступна, следующая пачка недоступна тоже.
            logger.error(
                "векторы не посчитаны (%s): записано %d из %d, отчёт выйдет без семантики. "
                "Ключевая оценка не затронута, следующий прогон попробует снова",
                error,
                written,
                len(pending),
            )
            return written
        for (vacancy_id, _), vector in zip(batch, vectors, strict=True):
            repo.save_embedding(vacancy_id, model, pack_vector(vector))
            written += 1
    logger.info("посчитано векторов описаний: %d", written)
    return written


def build_ranker(
    client: OllamaClient, profile: ProfileConfig, model: str
) -> "SemanticRanker | None":
    """Вектор профиля на этот прогон. `None` — семантики не будет.

    Считается КАЖДЫЙ прогон и не кэшируется на диске: он стоит одного
    запроса (~280 мс, замер §0.5), а кэш пришлось бы обесценивать при
    каждой правке `profile.yaml` — то есть хранить рядом ещё и отпечаток
    профиля. Расход, которого нет, дешевле сторожа, который может
    разъехаться.
    """
    try:
        vectors = client.embed([profile_text(profile)])
    except LlmUnavailable as error:
        logger.error(
            "вектор профиля не посчитан (%s): отчёт выйдет в прежнем порядке, "
            "по одной ключевой оценке",
            error,
        )
        return None
    return SemanticRanker(profile_vector=vectors[0], model=model)


@dataclass(frozen=True)
class SemanticRanker:
    """Косинус к профилю для вакансий, уходящих в отчёт."""

    profile_vector: list[float]
    model: str

    def attach(self, repo: Repository, vacancies: Sequence[ScoredVacancy]) -> list[ScoredVacancy]:
        """Проставить `semantic` там, где вектор есть и он сравним.

        Порча одной записи стоит ей семантики и ничего больше: вакансия
        уходит в конец своей связки, а не пропадает вместе с отчётом. Цена
        несопоставима, и потому оба исключения `llm/semantic.py` —
        оборванный BLOB и чужая размерность — ловятся здесь по одной
        вакансии, а не вокруг всего цикла.
        """
        blobs = repo.embeddings([item.discovered.id for item in vacancies], self.model)
        attached = []
        for item in vacancies:
            blob = blobs.get(item.discovered.id)
            similarity = None if blob is None else self._similarity(item.discovered.id, blob)
            attached.append(
                item if similarity is None else item.model_copy(update={"semantic": similarity})
            )
        return attached

    def _similarity(self, vacancy_id: str, blob: bytes) -> float | None:
        try:
            return cosine(self.profile_vector, unpack_vector(blob))
        except ValueError as error:
            logger.warning(
                "вектор вакансии %s непригоден (%s): она уйдёт в отчёт без семантики",
                vacancy_id,
                error,
            )
            return None
