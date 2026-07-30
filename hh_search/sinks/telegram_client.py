"""Транспорт Bot API: учётные данные, `sendMessage`/`sendDocument`.

К `api.telegram.org` идёт обычный `httpx`, а НЕ вежливый клиент из
`sources/http.py`: тот проверяет `robots.txt` и держит паузу
`delay_between_requests_sec` под hh.ru. Применять его здесь означало бы и
бессмысленную проверку чужого `robots.txt`, и паузу вежливости там, где
вежливость измеряется иначе.

Токен лежит в ПУТИ URL (`/bot<ТОКЕН>/sendMessage`), а `httpx` кладёт URL в
текст своих исключений. Поэтому наружу они не выпускаются ни при каких
обстоятельствах: ловятся и заменяются `TelegramError`, в которой стоит имя
метода. Сторожат это тесты в `tests/test_telegram_sink.py`.

Ловится `Exception`, а не `httpx.HTTPError`: `httpx.InvalidURL` (URL с
управляющим символом — правдоподобно при кривом `.env`, где `.strip()` в
`TelegramCredentials.from_env` чистит только края строки) наследует не
`HTTPError`, а напрямую `Exception` — перечисление подклассов `httpx`
пропустило бы его наружу вместе с URL, то есть токеном.

`TelegramSink` (приёмник, `hh_search/sinks/telegram_sink.py`) вынесен в
отдельный модуль намеренно: он зовёт только эти два публичных метода, не
касаясь токена и URL — ровно так же, как приёмники `csv`/`markdown` зовут
чистые функции `html_report.py`, не зная о транспорте вообще.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from hh_search.logging_setup import redact_secret

API_ROOT = "https://api.telegram.org"
# Потолок `sendMessage` у Bot API. Держим здесь, а не в конфиге: это не
# наша настройка, а чужое ограничение, и менять его нам нечем.
MESSAGE_LIMIT = 4096
# Потолок подписи к документу — там же и по той же причине.
CAPTION_LIMIT = 1024
# Чем токен заменяется в записи лога. Не пустотой: человек, читающий лог,
# обязан видеть, что здесь стояло и почему исчезло.
REDACTED = "/bot<ТОКЕН СКРЫТ>/"


def message_length(text: str) -> int:
    """Длина так, как её считает Telegram: в кодовых единицах UTF-16.

    Не в кодовых точках. Всё вне BMP — эмодзи в заголовке вакансии, а они
    там встречаются, — занимает две единицы: 300 записей дали `len(text)`
    = 3994 при 5494 по счёту Bot API, то есть 400 на запрос. Дальше яд с
    самоподдержкой: `send_message` падает, файл дня не публикуется,
    следующий прогон собирает то же сообщение и падает снова — и канал не
    получает НИЧЕГО, пока порог не сдвинут руками.
    """
    return len(text.encode("utf-16-le")) // 2


class TelegramError(RuntimeError):
    """Отказ Bot API или транспорта. Текст НИКОГДА не содержит токена."""


def _guard_logs(token: str) -> None:
    """Зарегистрировать токен как секрет, обязанный вычищаться из логов.

    Сама фильтрация (`TokenFilter`) и её независимость от порядка вызовов
    живут в `logging_setup.py` (спека §4, находка item 6): та же угроза,
    защита нужна не только Telegram-клиенту, поэтому и живёт не здесь.
    `httpx` пишет `HTTP Request: POST <URL> "200 OK"` на КАЖДЫЙ успешный
    запрос, а токен лежит в ПУТИ URL — без фильтра на обработчиках один
    вечер отладки (`logging.getLogger("httpx").setLevel(logging.INFO)`)
    вынес бы пароль бота в `data/logs/hh.log`, файл, который человек первым
    делом кому-нибудь показывает.
    """
    redact_secret(f"/bot{token}/", REDACTED)


@dataclass(frozen=True)
class TelegramCredentials:
    token: str
    chat_id: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TelegramCredentials":
        """Секреты из окружения. Отсутствие любого — отказ на старте.

        Пустая строка и пробелы отвергаются наравне с отсутствием: `.env` с
        `TELEGRAM_CHAT_ID=` — самая частая форма недописанной настройки, и
        отказ по ней обязан случиться до сети, а не 400-й от Telegram
        посреди прогона.

        В текст ошибки не подставляется ЗНАЧЕНИЕ переменной — только имя.
        """
        missing = [
            name
            for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not env.get(name, "").strip()
        ]
        if missing:
            raise ValueError(
                f"приёмник telegram включён, но не задано: {', '.join(missing)}. "
                "Переменные читаются из окружения (в контейнер приезжают из .env)"
            )
        return cls(
            token=env["TELEGRAM_BOT_TOKEN"].strip(),
            chat_id=env["TELEGRAM_CHAT_ID"].strip(),
        )


class TelegramClient:
    """Два метода Bot API и ни одного лишнего."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        timeout_sec: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout_sec = timeout_sec
        self._transport = transport
        _guard_logs(credentials.token)

    def send_message(self, text: str) -> None:
        self._call(
            "sendMessage",
            data={
                "chat_id": self._credentials.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        self._call(
            "sendDocument",
            data={"chat_id": self._credentials.chat_id, "caption": caption[:CAPTION_LIMIT]},
            files={"document": (filename, content, "text/html")},
        )

    def _call(
        self,
        method: str,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> None:
        url = f"{API_ROOT}/bot{self._credentials.token}/{method}"
        response: httpx.Response | None = None
        failure = ""
        try:
            with httpx.Client(timeout=self._timeout_sec, transport=self._transport) as http:
                response = http.post(url, data=data, files=files)
        except Exception as error:  # noqa: BLE001 — см. докстринг модуля: не
            # только `httpx.HTTPError`, но и `httpx.InvalidURL` (отдельная
            # ветка наследования от `Exception`) обязаны быть перехвачены,
            # а перечисление конкретных подклассов `httpx` ненадёжно на
            # будущее. `error` МОЖЕТ содержать URL, то есть токен — наружу
            # уходит только тип исключения.
            failure = type(error).__name__
        if response is None:
            # Поднимается ВНЕ блока `except` намеренно: внутри него
            # `__context__` новой ошибки держал бы исходное исключение
            # httpx вместе с URL, то есть с токеном, и `exc_info=True` в
            # `emit_to_sinks` вывел бы его в лог. `from None` гасит только
            # показ цепочки, а ссылка остаётся; здесь её нет вовсе.
            raise TelegramError(f"{method}: транспорт отказал ({failure})")
        payload = _payload(response)
        if response.status_code != httpx.codes.OK or not _is_ok(payload):
            # Bot API возвращает смысловой отказ (`{"ok": false, ...}`) и
            # при HTTP 200 — статус один не решает успех, решает поле `ok`.
            raise TelegramError(f"{method}: {response.status_code}, {_description(payload)}")


def _payload(response: httpx.Response) -> object:
    """Тело ответа Bot API как JSON, либо `None`, если оно не разобралось."""
    try:
        return response.json()
    except ValueError:
        return None


def _is_ok(payload: object) -> bool:
    """Успех решает поле `ok` в теле, а не HTTP-статус (см. `_call`)."""
    return isinstance(payload, dict) and payload.get("ok") is True


def _description(payload: object) -> str:
    """Человеческая причина отказа из уже разобранного тела ответа Bot API."""
    if payload is None:
        return "тело ответа не разобрано"
    description = payload.get("description") if isinstance(payload, dict) else None
    return str(description) if description else "без описания"
