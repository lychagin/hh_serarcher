"""Приёмник `telegram`: транспорт Bot API и отправка отчёта.

К `api.telegram.org` идёт обычный `httpx`, а НЕ вежливый клиент из
`sources/http.py`: тот проверяет `robots.txt` и держит паузу
`delay_between_requests_sec` под hh.ru. Применять его здесь означало бы и
бессмысленную проверку чужого `robots.txt`, и паузу вежливости там, где
вежливость измеряется иначе.

Токен лежит в ПУТИ URL (`/bot<ТОКЕН>/sendMessage`), а `httpx` кладёт URL в
текст своих исключений. Поэтому наружу они не выпускаются ни при каких
обстоятельствах: ловятся и заменяются `TelegramError`, в которой стоит имя
метода. Сторожат это три теста в `tests/test_telegram_sink.py`.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
# Потолок `sendMessage` у Bot API. Держим здесь, а не в конфиге: это не
# наша настройка, а чужое ограничение, и менять его нам нечем.
MESSAGE_LIMIT = 4096
# Потолок подписи к документу — там же и по той же причине.
CAPTION_LIMIT = 1024


class TelegramError(RuntimeError):
    """Отказ Bot API или транспорта. Текст НИКОГДА не содержит токена."""


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
        try:
            with httpx.Client(timeout=self._timeout_sec, transport=self._transport) as http:
                response = http.post(url, data=data, files=files)
        except httpx.HTTPError as error:
            # `error` СОДЕРЖИТ URL, то есть токен. Наружу уходит только тип.
            raise TelegramError(f"{method}: транспорт отказал ({type(error).__name__})") from None
        if response.status_code != httpx.codes.OK:
            raise TelegramError(f"{method}: {response.status_code}, {_description(response)}")


def _description(response: httpx.Response) -> str:
    """Человеческая причина отказа из тела ответа Bot API."""
    try:
        payload = response.json()
    except ValueError:
        return "тело ответа не разобрано"
    description = payload.get("description") if isinstance(payload, dict) else None
    return str(description) if description else "без описания"
