"""HTTP-клиент локальной Ollama.

`PoliteClient` здесь намеренно не переиспользуется (§3 спеки
`docs/superpowers/specs/2026-08-26-local-llm-design.md`): он про
robots.txt, паузу в секунду между запросами и `Retry-After` — три вещи,
которых у демона на соседней машине нет. Унаследовать их значило бы
платить двести секунд пауз за прогон ни за что и спрашивать robots.txt у
собственного localhost.
"""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

import httpx

from hh_search.config.models import LlmConfig
from hh_search.errors import LlmUnavailable

# Порт Ollama по умолчанию. Участвует только в сборке адреса из шлюза:
# явный `base_url` из конфига проходит мимо и порт называет сам.
DEFAULT_PORT = 11434

_ROUTE_FILE = Path("/proc/net/route")

# Переопределение адреса средой. Старше конфига сознательно: адрес зависит
# от того, ГДЕ запущен процесс, а не от того, что владелец решил про поиск
# работы. Один и тот же `app.yaml` едет в контейнер, где хост зовут
# `host.docker.internal`, и в WSL, где его надо вычислять из таблицы
# маршрутов.
BASE_URL_ENV = "HH_LLM_BASE_URL"

# Строка таблицы маршрутов с этим назначением — маршрут по умолчанию.
_DEFAULT_DESTINATION = "00000000"


def resolve_base_url(
    configured: str,
    route_file: Path = _ROUTE_FILE,
    environ: Mapping[str, str] | None = None,
) -> str:
    """`auto` — вычислить адрес хоста, всё остальное — вернуть как есть.

    `auto` существует потому, что адрес шлюза WSL2 в режиме NAT меняется
    при каждой перезагрузке Windows (замер 2026-08-26: 172.17.32.1).
    Записанный руками адрес протухал бы МОЛЧА: прогон не упал бы, а
    деградировал на одни ключевые слова (§4 спеки), и владелец принял бы
    строку лога за выключенный Windows.
    """
    # Пустая переменная — не адрес: `HH_LLM_BASE_URL=` в `.env` читается
    # человеком как «оставить по умолчанию», а голым `get` дало бы
    # `httpx.Client(base_url="")` и запросы по относительному пути в никуда.
    override = (environ if environ is not None else os.environ).get(BASE_URL_ENV, "").strip()
    if override:
        return override
    if configured != "auto":
        return configured
    return f"http://{_default_gateway(route_file)}:{DEFAULT_PORT}"


def _default_gateway(route_file: Path) -> str:
    """Адрес шлюза по умолчанию из таблицы маршрутов ядра.

    Порядок байт в колонке `Gateway` — little-endian, и это не деталь
    разбора: `012011AC`, прочитанный как big-endian, даёт 1.32.17.172 —
    существующий чужой адрес в интернете, а не хост-машину. Запрос ушёл бы
    наружу и получил бы отказ, неотличимый от выключенного Ollama.
    """
    for line in route_file.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split()
        # Маршрут по умолчанию без шлюза (point-to-point) даёт 0.0.0.0 —
        # адрес, по которому никто не отвечает. Он отвергается здесь же, а
        # не превращается в base_url, до которого не достучаться.
        if len(fields) < 3 or fields[1] != _DEFAULT_DESTINATION:
            continue
        if fields[2] == _DEFAULT_DESTINATION:
            continue
        packed = bytes.fromhex(fields[2])
        return ".".join(str(byte) for byte in reversed(packed))
    raise ValueError(
        f"llm.base_url: auto — в {route_file} нет маршрута по умолчанию со шлюзом, "
        "вычислить адрес хоста Ollama не из чего. Укажите адрес явно"
    )


class OllamaClient:
    """Два вызова к локальной Ollama: `/api/chat` и `/api/embed`.

    Повторов нет. У `PoliteClient` они есть, потому что hh.ru отвечает
    `429` и `Retry-After`, а страница вакансии стоит денег: потерять её —
    значит потерять вакансию. Здесь терять нечего: отказ означает оценку
    по одним ключевым словам (§4 спеки), вакансия остаётся в базе, и
    следующий прогон через четыре часа попробует снова. Повтор в этих
    условиях — способ утроить время прогона на выключенной машине.
    """

    def __init__(
        self,
        config: LlmConfig,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        # Адрес разрешён ВЫШЕ и передан готовым: `auto` умеет падать
        # (§2 спеки), а падать конструктор клиента обязан не в середине
        # прогона, а на старте процесса, вместе с остальным конфигом.
        self._client = httpx.Client(
            base_url=base_url,
            timeout=config.timeout_sec,
            transport=transport,
        )

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self._config.embed_model, "input": list(texts)}
        body = self._post("/api/embed", payload)
        vectors = body.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise LlmUnavailable(
                f"/api/embed вернул {len(vectors) if isinstance(vectors, list) else 'не список'} "
                f"векторов на {len(texts)} текстов"
            )
        return [[float(value) for value in vector] for vector in vectors]

    def chat(self, system: str, user: str, schema: Mapping[str, object]) -> dict[str, object]:
        """Один ответ по JSON-схеме. Схема — ОБЪЕКТ, а не строка `"json"`.

        Замер 2026-08-26 (§0.4 спеки): со свободным `format: "json"` та же
        модель на том же входе вернула грейд `"middle+/senior"` мимо
        перечисления, со схемой объектом — `"middle"`. Разница не в
        промпте, а в том, чем Ollama ограничивает генерацию.
        """
        payload = {
            "model": self._config.chat_model,
            "stream": False,
            "format": dict(schema),
            # Ноль, а не умолчание: одна и та же вакансия обязана давать
            # одну и ту же оценку в двух прогонах, иначе `rescore` начнёт
            # менять порядок отчёта без единой правки конфига.
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        content = self._post("/api/chat", payload).get("message", {})
        text = content.get("content") if isinstance(content, dict) else None
        if not isinstance(text, str):
            raise LlmUnavailable("/api/chat вернул ответ без message.content")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise LlmUnavailable(f"/api/chat вернул не JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise LlmUnavailable(f"/api/chat вернул {type(parsed).__name__}, а не объект")
        return parsed

    def _post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        """Любой отказ транспорта — один и тот же `LlmUnavailable`.

        `httpx.HTTPError` накрывает и таймаут, и отказ соединения, и
        разорванный ответ. Перечислять их по одному значило бы завести
        список, который разойдётся со следующей версией httpx молча.
        """
        try:
            response = self._client.post(path, json=payload)
        except httpx.HTTPError as error:
            raise LlmUnavailable(f"{path}: {type(error).__name__}: {error}") from error
        if response.status_code != httpx.codes.OK:
            raise LlmUnavailable(f"{path}: код {response.status_code}, {_server_words(response)}")
        try:
            body = response.json()
        except ValueError as error:
            raise LlmUnavailable(f"{path}: тело ответа не разобралось как JSON: {error}") from error
        if not isinstance(body, dict):
            raise LlmUnavailable(f"{path}: тело ответа не объект, а {type(body).__name__}")
        return body


def _server_words(response: httpx.Response) -> str:
    """Жалоба Ollama её собственными словами, как она их написала.

    Замер 2026-08-26: на не скачанную модель Ollama отдаёт `404` и тело
    `{"error": "model \'X\' not found, try pulling it first"}` — ровно ту
    строку, которая говорит владельцу, что делать. Стандартный текст
    `HTTPStatusError` вместо неё называет URL и ссылку на MDN, чем
    отправляет искать сетевую проблему там, где опечатка в `llm.chat_model`.

    Тело берётся сырым, а не полем `error` из разобранного JSON: разбора
    это стоило бы отдельной ветки, а несёт ту же строку — мутация,
    заменявшая поле на `None`, не красила ни одного теста именно потому,
    что запасной путь давал тот же ответ.
    """
    return response.text[:200]
