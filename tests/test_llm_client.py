"""Клиент локальной Ollama: адрес, вызовы, отказы.

Отдельный файл от `test_http.py` по той же причине, по какой
`OllamaClient` — отдельный класс от `PoliteClient`: у локального демона
нет ни robots.txt, ни паузы вежливости, ни `Retry-After`, и общих
инвариантов у них ровно ноль.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.models import LlmConfig
from hh_search.errors import LlmUnavailable
from hh_search.llm.client import BASE_URL_ENV, OllamaClient, resolve_base_url

# Живой /proc/net/route этой машины (WSL2, NAT). Шлюз `012011AC` —
# little-endian запись 172.17.32.1, и порядок байт здесь не украшение:
# прочитанный как big-endian, тот же шлюз дал бы 1.32.17.172, то есть
# существующий чужой адрес в интернете, а не хост-машину.
WSL_ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t012011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t002011AC\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"
)


def test_auto_resolves_the_default_gateway(tmp_path: Path) -> None:
    route = tmp_path / "route"
    route.write_text(WSL_ROUTE, encoding="utf-8")

    assert resolve_base_url("auto", route_file=route) == "http://172.17.32.1:11434"


def test_explicit_url_is_returned_as_is(tmp_path: Path) -> None:
    route = tmp_path / "route"
    route.write_text(WSL_ROUTE, encoding="utf-8")

    assert resolve_base_url("http://ollama:11434", route_file=route) == "http://ollama:11434"


def test_auto_without_a_default_route_is_a_configuration_error(tmp_path: Path) -> None:
    """Нет маршрута по умолчанию — сказать об этом, а не молча взять localhost.

    Молчаливый `127.0.0.1` был бы худшим из исходов: в WSL там никого нет,
    прогон деградировал бы на ключевые слова (§4 спеки) и владелец узнал бы
    о потере из одной строки лога, приняв её за выключенный Windows.
    """
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\neth0\t002011AC\t00000000\t0001\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="маршрут"):
        resolve_base_url("auto", route_file=route)


def test_default_route_without_a_gateway_is_not_taken_for_an_address(tmp_path: Path) -> None:
    """Маршрут по умолчанию бывает без шлюза (point-to-point) — это не адрес.

    Отдельный тест от соседнего сверху, хотя оба ждут `ValueError`: тот
    подаёт таблицу БЕЗ маршрута по умолчанию, а эта ветка исполняется
    только маршрутом по умолчанию, у которого шлюз нулевой. Проверено
    мутацией: без этого теста снятие отсева не красило ничего, и
    `resolve_base_url` молча отдавал бы `http://0.0.0.0:11434`.
    """
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\nwg0\t00000000\t00000000\t0001\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="маршрут"):
        resolve_base_url("auto", route_file=route)


def test_environment_overrides_the_configured_address(tmp_path: Path) -> None:
    """Переменная старше конфига, и это не вкус.

    Адрес зависит от того, ГДЕ запущен процесс, а не от того, что владелец
    решил про поиск работы: тот же самый `app.yaml` едет в контейнер, где
    хост зовут `host.docker.internal`, и в WSL, где его надо вычислять из
    таблицы маршрутов. Конфиг описывает намерение, переменная — среду.
    """
    route = tmp_path / "route"
    route.write_text(WSL_ROUTE, encoding="utf-8")

    resolved = resolve_base_url(
        "auto",
        route_file=route,
        environ={BASE_URL_ENV: "http://host.docker.internal:11434"},
    )

    assert resolved == "http://host.docker.internal:11434"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_environment_value_does_not_override(value: str, tmp_path: Path) -> None:
    """Пустая или пробельная переменная — это не адрес.

    `HH_LLM_BASE_URL=` в `.env` выглядит как «оставить по умолчанию», а
    голое чтение отдало бы пустую строку в `httpx.Client(base_url="")`,
    после чего каждый запрос ушёл бы по относительному пути в никуда.

    Пробельные значения перечислены рядом с пустым не для полноты: `.env`
    правится руками, хвостовой пробел в нём невидим, и без `strip` такой
    адрес прошёл бы в `httpx` целиком. Проверено мутацией — на одном лишь
    `""` снятие `strip` не красило ничего.
    """
    route = tmp_path / "route"
    route.write_text(WSL_ROUTE, encoding="utf-8")

    assert (
        resolve_base_url("auto", route_file=route, environ={BASE_URL_ENV: value})
        == "http://172.17.32.1:11434"
    )


# --- Клиент ---------------------------------------------------------------

BASE = "http://ollama.test:11434"


def make_client(**overrides: object) -> OllamaClient:
    config = LlmConfig(base_url=BASE, chat_model="llama3", embed_model="bge-m3", timeout_sec=5)
    return OllamaClient(config.model_copy(update=overrides), base_url=BASE)


GRADE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"seniority": {"type": "string", "enum": ["middle", "senior"]}},
    "required": ["seniority"],
}


@respx.mock
def test_embed_returns_one_vector_per_text() -> None:
    respx.post(f"{BASE}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})
    )

    assert make_client().embed(["профиль", "вакансия"]) == [[1.0, 0.0], [0.0, 1.0]]


@respx.mock
def test_chat_sends_the_schema_as_an_object_and_not_as_the_string_json() -> None:
    """Замер 2026-08-26 (§0.4 спеки), из-за которого этот тест существует.

    Со свободным `format: "json"` llama3 вернула грейд `"middle+/senior"` —
    мимо перечисления. С тем же промптом и схемой ОБЪЕКТОМ вернула
    `"middle"`. Мутация, которую тест обязан ловить, ровно одна: замена
    объекта схемы на строку `"json"`, после которой всё продолжает
    работать и молча портит данные.
    """
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": '{"seniority": "middle"}'}})
    )

    assert make_client().chat("извлеки", "вакансия", GRADE_SCHEMA) == {"seniority": "middle"}

    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == GRADE_SCHEMA
    assert sent["stream"] is False
    assert sent["model"] == "llama3"
    # Ноль, а не умолчание Ollama: одна и та же вакансия обязана давать один
    # и тот же ответ в двух прогонах, иначе `rescore` менял бы порядок
    # отчёта без единой правки конфига. Проверено мутацией.
    assert sent["options"]["temperature"] == 0


@respx.mock
def test_unreachable_ollama_raises_llm_unavailable() -> None:
    respx.post(f"{BASE}/api/embed").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(LlmUnavailable):
        make_client().embed(["профиль"])


@respx.mock
def test_timeout_raises_llm_unavailable() -> None:
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(LlmUnavailable):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)


@respx.mock
def test_http_error_status_raises_llm_unavailable() -> None:
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(404, text="model not found"))

    with pytest.raises(LlmUnavailable):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)


@respx.mock
def test_content_that_is_not_json_raises_llm_unavailable() -> None:
    """Мусор вместо JSON — тот же отказ, что и недоступность.

    Один тип ошибки на все виды «LLM не дала пригодного ответа» выбран
    сознательно: конвейер обязан поступать с ними одинаково (§4 спеки) —
    посчитать по ключевым словам и идти дальше. Два типа означали бы две
    ветки обработки, различающиеся ничем.
    """
    respx.post(f"{BASE}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "Конечно! Вот ответ:"}})
    )

    with pytest.raises(LlmUnavailable):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)


@respx.mock
def test_embeddings_missing_from_the_response_raises_llm_unavailable() -> None:
    respx.post(f"{BASE}/api/embed").mock(return_value=httpx.Response(200, json={"error": "oops"}))

    with pytest.raises(LlmUnavailable):
        make_client().embed(["профиль"])


@respx.mock
def test_fewer_vectors_than_texts_raises_llm_unavailable() -> None:
    """Векторов меньше, чем текстов, — это не «частичный успех», а порча.

    Вектора возвращаются позиционно, и молча принять два вектора на три
    текста значит приписать вакансии чужой вектор. Ошибка при этом не
    падает никогда: косинус посчитается, число получится правдоподобное, а
    отчёт отранжируется по чужому смыслу. Проверено мутацией: без этого
    теста снятие сверки длин не красило ничего — соседний тест подаёт
    ответ вовсе без ключа `embeddings` и до сверки не доходит.
    """
    respx.post(f"{BASE}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})
    )

    with pytest.raises(LlmUnavailable, match="2"):
        make_client().embed(["профиль", "вакансия"])


@respx.mock
def test_missing_model_is_reported_with_the_servers_own_words() -> None:
    """Не скачанная модель — самый вероятный реальный отказ, и он обязан читаться.

    Тело ответа взято из замера 2026-08-26: Ollama отдаёт `404` и ВАЛИДНЫЙ
    JSON. То есть без проверки кода ответа разбор идёт дальше и сообщает
    «ответ без message.content» — про опечатку в `llm.chat_model` владелец
    из этого не узнает ничего. Проверено мутацией: снятие проверки кода не
    красило ни одного теста, потому что все прочие пути отказа приводили к
    тому же типу исключения с бесполезным текстом.
    """
    respx.post(f"{BASE}/api/chat").mock(
        return_value=httpx.Response(404, json={"error": "model 'llama3' not found"})
    )

    with pytest.raises(LlmUnavailable, match=r"404.*model 'llama3' not found"):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)


@respx.mock
def test_content_that_is_json_but_not_an_object_raises_llm_unavailable() -> None:
    """Схема обещает объект, а разбор может дать список — и тип соврал бы.

    Не теоретический вход: измерено (§0.4 спеки), что при `format: "json"`
    та же модель уже отдавала значение мимо объявленного перечисления.
    Ollama — внешняя система, и `chat`, объявленный возвращающим `dict`,
    обязан сам за это отвечать, а не полагаться на её дисциплину.
    """
    respx.post(f"{BASE}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "[1, 2]"}})
    )

    with pytest.raises(LlmUnavailable, match="list"):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)


@respx.mock
def test_response_without_message_content_raises_llm_unavailable() -> None:
    """Ответ без `message.content` — отказ, а не `TypeError` из глубины разбора.

    Проверено мутацией: без этого теста снятие проверки типа не красило
    ничего, а в живом прогоне `json.loads(None)` дал бы `TypeError` —
    исключение, которого нет ни в одном обработчике конвейера, то есть
    недоступная модель роняла бы прогон вместо деградации (§4 спеки).
    """
    respx.post(f"{BASE}/api/chat").mock(return_value=httpx.Response(200, json={"message": {}}))

    with pytest.raises(LlmUnavailable, match="message.content"):
        make_client().chat("извлеки", "вакансия", GRADE_SCHEMA)
