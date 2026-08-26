"""Центральный инвариант §4 спеки, проверенный ИСПОЛНЕНИЕМ конвейера.

Модульного теста здесь мало по правилу проекта: `WorkFormatBlockStats`
уже был покрыт модульным тестом, зелен и НЕ подключён к конвейеру, а
модульный тест этой дыры не видел, потому что звал метод напрямую.
Поэтому всё ниже идёт через `run_once` целиком.

Утверждение одно и сформулировано как сравнение: прогон с недоступной
моделью обязан дать РОВНО то же, что прогон вовсе без модели. Не «не
упасть» — совпасть.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.loader import load_config
from hh_search.config.models import Config, LlmConfig
from hh_search.llm.client import OllamaClient
from hh_search.pipeline import run_once
from hh_search.pipeline.stats import RunStats
from hh_search.scoring.keyword import KeywordScorer
from hh_search.storage.repository import SqliteRepository
from tests.test_config import APP_YAML, write_config
from tests.test_pipeline import (
    NOW,
    ONE_PAGE,
    TWO_ENRICHABLE,
    RecordingSink,
    make_client,
    mock_source,
)

OLLAMA = "http://ollama.test:11434"

# Пять способов сломаться, которые respx умеет изобразить: исключение
# транспорта или готовый ответ. Общий тип нужен `mypy --strict` —
# параметризация иначе выводится в `object`, который respx не принимает.
Failure = httpx.Response | Exception


# Семантика ВКЛЮЧЕНА явно. Умолчание `LlmConfig` — `false` (обновление не
# включает возможность само), и на нём все сторожа этого файла проходили
# бы вхолостую: шаг просто не звался бы. Проверено мутацией — с базовым
# APP_YAML отключение всего шага целиком не красило ни одного теста.
LLM_ON = """
llm:
  base_url: "http://ollama.test:11434"
  embed_model: bge-m3
  chat_model: llama3
  semantic: true
  facts: true
"""


def fresh_config(tmp_path: Path, name: str) -> Config:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return load_config(
        write_config(root, **{"queries.yaml": ONE_PAGE, "app.yaml": APP_YAML + LLM_ON})
    )


def fresh_repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def llm_client() -> OllamaClient:
    return OllamaClient(
        LlmConfig(base_url=OLLAMA, embed_model="bge-m3", timeout_sec=5), base_url=OLLAMA
    )


def outcome(sink: RecordingSink) -> list[tuple[str, float]]:
    """То, что видит владелец: порядок вакансий и их оценки."""
    return [(item.discovered.id, item.score.total) for item in sink.items]


@respx.mock
def run_with_llm(config: Config, failure: Failure) -> tuple[RecordingSink, RunStats]:
    mock_source(TWO_ENRICHABLE)
    # ОБА конца ломаются одинаково: отказала модель, а не одна её ручка.
    # Мокать только `/api/embed` значило бы проверять деградацию половины
    # шага, а вторая половина — `/api/chat` — роняла бы прогон невидимо.
    for path in ("/api/embed", "/api/chat"):
        route = respx.post(f"{OLLAMA}{path}")
        # Отказ транспорта respx изображает исключением, отказ сервера —
        # готовым ответом, и подать одно вместо другого он не даёт.
        if isinstance(failure, httpx.Response):
            route.mock(return_value=failure)
        else:
            route.mock(side_effect=failure)
    sink = RecordingSink()
    repo = fresh_repo()
    with make_client(config) as client:
        stats = run_once(
            config, client, repo, KeywordScorer(config.profile), [sink], NOW, llm=llm_client()
        )
    return sink, stats


@respx.mock
def run_without_llm(config: Config) -> tuple[RecordingSink, RunStats]:
    mock_source(TWO_ENRICHABLE)
    sink = RecordingSink()
    repo = fresh_repo()
    with make_client(config) as client:
        stats = run_once(config, client, repo, KeywordScorer(config.profile), [sink], NOW)
    return sink, stats


@pytest.mark.parametrize(
    ("name", "failure"),
    [
        ("соединение отвергнуто", httpx.ConnectError("connection refused")),
        ("таймаут чтения", httpx.ReadTimeout("too slow")),
        ("модель не скачана", httpx.Response(404, json={"error": "model not found"})),
        ("мусор вместо тела", httpx.Response(200, text="<html>proxy error</html>")),
        ("векторов меньше, чем текстов", httpx.Response(200, json={"embeddings": []})),
    ],
)
def test_broken_ollama_changes_nothing_at_all(name: str, failure: Failure, tmp_path: Path) -> None:
    """Пять способов сломаться — и ни один не меняет ни одного вердикта."""
    with_llm, with_stats = run_with_llm(fresh_config(tmp_path, "a"), failure)
    without_llm, without_stats = run_without_llm(fresh_config(tmp_path, "b"))

    assert outcome(with_llm) == outcome(without_llm), name
    assert with_stats.status == without_stats.status == "ok", name


def test_broken_ollama_does_not_degrade_the_run_status(tmp_path: Path) -> None:
    """Отказ локальной модели НЕ красит прогон в partial.

    По той же причине, по которой его не красит отказ `Sink.maintain`:
    модель живёт на рабочей машине владельца, которая выключается на ночь.
    Крась она статус — `partial` стоял бы каждый прогон подряд, и статус
    перестал бы значить что-либо ровно там, где он единственный индикатор.
    """
    _, stats = run_with_llm(fresh_config(tmp_path, "c"), httpx.ConnectError("refused"))

    assert stats.status == "ok"
    assert stats.error is None


@respx.mock
def test_working_ollama_actually_reaches_the_report(tmp_path: Path) -> None:
    """Положительный сторож: без него все проверки выше проходят вхолостую.

    Тест вида «сломанная модель ничего не меняет» по построению не
    отличает сломанную модель от НЕПОДКЛЮЧЁННОГО шага: в обоих случаях
    разницы нет. Проверено мутацией — отключение `_rank_semantically`
    целиком не красило ни одного теста этого файла, пока здесь не
    появился он. Ровно на этом классе проект горел с
    `WorkFormatBlockStats`: сторож был зелен и не подключён.
    """
    config = fresh_config(tmp_path, "живая")
    mock_source(TWO_ENRICHABLE)
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"embeddings": [[1.0, 0.0]] * len(json.loads(request.content)["input"])}
        )
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": json.dumps({"stack": []})}})
    )
    sink = RecordingSink()
    repo = fresh_repo()

    with make_client(config) as client:
        run_once(config, client, repo, KeywordScorer(config.profile), [sink], NOW, llm=llm_client())

    assert sink.items, "прогон ничего не отправил — проверять нечего"
    assert all(item.semantic is not None for item in sink.items)
    assert set(repo.embeddings([item.discovered.id for item in sink.items], "bge-m3"))


@respx.mock
def test_working_ollama_puts_extracted_facts_into_the_report(tmp_path: Path) -> None:
    """Положительный сторож для фактов — брат-близнец сторожа выше.

    По той же причине: тесты деградации не отличают сломанную модель от
    неподключённого шага, и без этого теста удаление `extract_pending` из
    `run_once` целиком не красило бы ничего.
    """
    config = fresh_config(tmp_path, "факты")
    mock_source(TWO_ENRICHABLE)
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"embeddings": [[1.0, 0.0]] * len(json.loads(request.content)["input"])}
        )
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps({"stack": ["Yocto", "ARM"], "seniority": "senior"})
                }
            },
        )
    )
    sink = RecordingSink()
    repo = fresh_repo()

    with make_client(config) as client:
        run_once(config, client, repo, KeywordScorer(config.profile), [sink], NOW, llm=llm_client())

    assert sink.items, "прогон ничего не отправил — проверять нечего"
    assert all(item.facts is not None for item in sink.items)
    assert sink.items[0].facts is not None
    assert sink.items[0].facts.stack == ["Yocto", "ARM"]
