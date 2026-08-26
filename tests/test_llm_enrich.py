"""Шаг конвейера: эмбеддинг очереди и семантика в отчёте.

Клиент здесь настоящий (`OllamaClient` поверх `respx`), а не заглушка:
проверяется в том числе то, как он переводит отказы транспорта в
`LlmUnavailable`, — а подменённый клиент проверял бы только сам себя.
"""

import json

import httpx
import pytest
import respx

from hh_search.config.models import LlmConfig
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.llm.client import OllamaClient
from hh_search.llm.semantic import pack_vector
from hh_search.pipeline.llm_enrich import (
    SemanticRanker,
    build_ranker,
    embed_pending,
    extract_pending,
)
from hh_search.storage.repository import SqliteRepository
from tests.test_llm_semantic import PROFILE

BASE = "http://ollama.test:11434"
MODEL = "bge-m3"


def make_client() -> OllamaClient:
    return OllamaClient(LlmConfig(base_url=BASE, embed_model=MODEL, timeout_sec=5), base_url=BASE)


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def enriched(repo: SqliteRepository, vacancy_id: str) -> None:
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


def scored(vacancy_id: str) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title="Ведущий разработчик",
            found_by_query="programmist",
        ),
        details=VacancyDetails(description="Yocto BSP ARM"),
        score=ScoreBreakdown(title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=87.3),
        cluster="backend",
    )


@respx.mock
def test_pending_vacancies_get_their_vectors(repo: SqliteRepository) -> None:
    enriched(repo, "1")
    enriched(repo, "2")
    respx.post(f"{BASE}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})
    )

    assert embed_pending(make_client(), repo, MODEL, limit=10) == 2
    assert set(repo.embeddings(["1", "2"], MODEL)) == {"1", "2"}


@respx.mock
def test_unreachable_ollama_embeds_nothing_and_does_not_raise(repo: SqliteRepository) -> None:
    """Центральный инвариант §4 спеки: выключенный Windows не роняет прогон."""
    enriched(repo, "1")
    respx.post(f"{BASE}/api/embed").mock(side_effect=httpx.ConnectError("connection refused"))

    assert embed_pending(make_client(), repo, MODEL, limit=10) == 0
    assert repo.embeddings(["1"], MODEL) == {}


@respx.mock
def test_failure_is_logged_once_per_run_and_not_once_per_vacancy(
    repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Двести строк ERROR о недоступном localhost — это не диагностика.

    Лог — единственный канал наблюдаемости этого сервиса, и утопить в нём
    настоящую находку так же дорого, как её не записать.
    """
    for number in range(40):
        enriched(repo, str(number))
    respx.post(f"{BASE}/api/embed").mock(side_effect=httpx.ConnectError("connection refused"))

    with caplog.at_level("ERROR"):
        embed_pending(make_client(), repo, MODEL, limit=40)

    assert len([record for record in caplog.records if record.levelname == "ERROR"]) == 1


@respx.mock
def test_embedding_goes_in_batches(repo: SqliteRepository) -> None:
    """Все 40 описаний одним телом запроса — это мегабайты в одном POST.

    Пачками не ради красоты: описание вакансии доходит до 4 КБ, и очередь
    в двести штук (умолчание `limits.llm_per_run`) дала бы запрос под
    мегабайт, который целиком пропадает от одного таймаута.
    """
    for number in range(40):
        enriched(repo, str(number))

    def one_vector_per_input(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * count})

    route = respx.post(f"{BASE}/api/embed").mock(side_effect=one_vector_per_input)

    embed_pending(make_client(), repo, MODEL, limit=40)

    assert route.call_count > 1


@respx.mock
def test_ranker_is_built_from_the_profile_vector() -> None:
    respx.post(f"{BASE}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})
    )

    ranker = build_ranker(make_client(), PROFILE, MODEL)

    assert ranker is not None
    assert ranker.profile_vector == [1.0, 0.0]


@respx.mock
def test_ranker_is_absent_when_ollama_is_down() -> None:
    respx.post(f"{BASE}/api/embed").mock(side_effect=httpx.ConnectError("connection refused"))

    assert build_ranker(make_client(), PROFILE, MODEL) is None


def test_attach_fills_semantic_from_the_stored_vector(repo: SqliteRepository) -> None:
    enriched(repo, "1")
    repo.save_embedding("1", MODEL, pack_vector([1.0, 0.0]))
    ranker = SemanticRanker(profile_vector=[1.0, 0.0], model=MODEL)

    attached = ranker.attach(repo, [scored("1")])

    assert attached[0].semantic == pytest.approx(1.0)


def test_attach_leaves_semantic_unset_without_a_vector(repo: SqliteRepository) -> None:
    enriched(repo, "1")
    ranker = SemanticRanker(profile_vector=[1.0, 0.0], model=MODEL)

    assert ranker.attach(repo, [scored("1")])[0].semantic is None


def test_corrupt_vector_costs_the_vacancy_its_semantics_and_nothing_more(
    repo: SqliteRepository,
) -> None:
    """Порча одной строки не имеет права ронять отправку ВСЕГО отчёта.

    Цена несопоставима: без семантики вакансия уходит в конец своей
    связки, без отчёта не уходит никуда ни она, ни остальные.
    """
    enriched(repo, "1")
    repo.save_embedding("1", MODEL, b"\x00\x01\x02")
    ranker = SemanticRanker(profile_vector=[1.0, 0.0], model=MODEL)

    assert ranker.attach(repo, [scored("1")])[0].semantic is None


def test_vector_of_a_different_dimension_costs_only_its_semantics(repo: SqliteRepository) -> None:
    """Та же длина в байтах, другая размерность — вектор чужой модели.

    Имя модели ловит смену через конфиг, но не ловит запись, сделанную
    моделью с тем же именем и другим выходом. `cosine` отвергает такую
    пару, и отвергнутой обязана остаться одна вакансия.
    """
    enriched(repo, "1")
    repo.save_embedding("1", MODEL, pack_vector([1.0, 0.0, 0.0]))
    ranker = SemanticRanker(profile_vector=[1.0, 0.0], model=MODEL)

    assert ranker.attach(repo, [scored("1")])[0].semantic is None


def test_absent_vectors_are_silent_while_broken_ones_are_not(
    repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Отсутствие вектора — штатное состояние, порча — находка. Шуметь вправе только вторая.

    Разница наблюдаема только в логе, и потому легко теряется: обе ветки
    оставляют `semantic` пустым. Но прогон при выключенном Ollama содержит
    сотни вакансий без вектора, и WARNING на каждую утопил бы в себе ту
    единственную строку, ради которой лог читают. Проверено мутацией:
    без этого теста слияние двух веток не красило ничего.
    """
    enriched(repo, "целая")
    enriched(repo, "битая")
    repo.save_embedding("битая", MODEL, b"\x00\x01\x02")
    ranker = SemanticRanker(profile_vector=[1.0, 0.0], model=MODEL)

    with caplog.at_level("WARNING"):
        ranker.attach(repo, [scored("целая"), scored("битая")])

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "битая" in warnings[0].getMessage()


def enriched_with(
    repo: SqliteRepository, vacancy_id: str, description: str, score: float = 87.3
) -> None:
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
        ScoreBreakdown(title=1, stack=1, responsibilities=1, domain=1, penalty=0, total=score),
    )


@respx.mock
def test_relocation_is_asked_only_where_the_text_mentions_it(repo: SqliteRepository) -> None:
    """Второй запрос — только у тех, где переезд уже найден словами.

    Замер 2026-08-26: таких девять из ста пятидесяти. Спрашивать всех
    значило бы удвоить цену шага ради ответа, который и так известен без
    сети, — пять минут прогона вместо пятнадцати секунд.
    """
    enriched_with(repo, "с-переездом", "Работа в Елабуге, компания помогает с переездом")
    enriched_with(repo, "без-переезда", "Удалённая работа, Yocto и ARM")

    def by_question(request: httpx.Request) -> httpx.Response:
        # Отвечать по СОДЕРЖАНИЮ запроса, а не по порядку: очередь
        # `pending_facts` отсортирована по дате, и список ответов,
        # привязанный к порядку вакансий, проверял бы сортировку вместо
        # предмета теста.
        system = json.loads(request.content)["messages"][0]["content"]
        answer = (
            {"kind": "required", "city": "Елабуга"} if "переезд" in system else {"stack": ["Yocto"]}
        )
        return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})

    route = respx.post(f"{BASE}/api/chat").mock(side_effect=by_question)

    extract_pending(make_client(), repo, "llama3", limit=10)

    # Три запроса на две вакансии: факты обеим, уточнение — одной.
    assert route.call_count == 3
    stored = repo.facts(["с-переездом", "без-переезда"], "llama3")
    assert stored["с-переездом"].relocation is not None
    assert stored["с-переездом"].relocation.city == "Елабуга"
    assert stored["без-переезда"].relocation is None


@respx.mock
def test_opinion_is_asked_only_above_the_report_threshold(repo: SqliteRepository) -> None:
    """Мнение показывается только в «Топе», значит и спрашивать его надо там.

    Замер §0.8 спеки: выше порога 34 вакансии из 573. Спрашивать всех —
    втрое дороже ради строк, которых владелец не увидит: секция
    «Остальное» в отчёте минимальна по его же решению.
    """
    enriched_with(repo, "выше-порога", "Yocto BSP ARM", score=87.3)
    enriched_with(repo, "ниже-порога", "Yocto BSP ARM", score=12.0)

    def by_question(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        answer = (
            {"score": 35, "reason": "чужой стек"} if "Оцени" in system else {"stack": ["Yocto"]}
        )
        return httpx.Response(200, json={"message": {"content": json.dumps(answer)}})

    respx.post(f"{BASE}/api/chat").mock(side_effect=by_question)

    extract_pending(make_client(), repo, "llama3", 10, PROFILE, threshold=60.0)

    stored = repo.facts(["выше-порога", "ниже-порога"], "llama3")
    assert stored["выше-порога"].opinion is not None
    assert stored["выше-порога"].opinion.score == 35
    assert stored["ниже-порога"].opinion is None
