"""Транспорт Telegram и приёмник: всё без сети, на подставном httpx.

Главный сторож здесь — про утечку токена. Он лежит в пути URL, а проект
логирует URL при отказах, поэтому первая же сетевая ошибка утащила бы
пароль бота в data/logs/hh.log — файл, который человек первым делом
кому-нибудь показывает.
"""

import logging

import httpx
import pytest

from hh_search.sinks.telegram_sink import (
    TelegramClient,
    TelegramCredentials,
    TelegramError,
)

TOKEN = "1234567890:AAHtestTOKENvalueMUSTneverLEAK"
CHAT_ID = "-1001234567890"


def credentials() -> TelegramCredentials:
    return TelegramCredentials(token=TOKEN, chat_id=CHAT_ID)


def client(handler: object) -> TelegramClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TelegramClient(credentials(), transport=transport)


def test_credentials_from_env_reads_both_variables() -> None:
    resolved = TelegramCredentials.from_env(
        {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
    )
    assert resolved.token == TOKEN
    assert resolved.chat_id == CHAT_ID


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TELEGRAM_BOT_TOKEN": TOKEN},
        {"TELEGRAM_CHAT_ID": CHAT_ID},
        {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": CHAT_ID},
        {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "   "},
    ],
)
def test_credentials_from_env_refuses_incomplete_environment(env: dict[str, str]) -> None:
    """Отказ обязан случиться на старте, а не в середине прогона (спека §4)."""
    with pytest.raises(ValueError):
        TelegramCredentials.from_env(env)


def test_credentials_error_does_not_contain_the_token() -> None:
    with pytest.raises(ValueError) as caught:
        TelegramCredentials.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    assert TOKEN not in str(caught.value)


def test_send_message_posts_text_and_chat_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client(handler).send_message("привет")
    assert "/sendMessage" in str(seen["url"])
    assert "chat_id" in str(seen["body"])


def test_send_message_asks_for_html_parse_mode() -> None:
    """MarkdownV2 требует экранировать скобки и точки, а они есть почти в
    каждом заголовке вакансии живого прогона (спека §2)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    client(handler).send_message("привет")
    assert "HTML" in seen["body"]


def test_send_document_uploads_file_content() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client(handler).send_document("2026-07-29-new.html", b"<h1>hi</h1>", "подпись")
    assert "/sendDocument" in str(seen["url"])
    assert b"<h1>hi</h1>" in bytes(seen["body"])  # type: ignore[call-overload]
    assert b"2026-07-29-new.html" in bytes(seen["body"])  # type: ignore[call-overload]


def test_api_error_becomes_telegram_error_without_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    with pytest.raises(TelegramError) as caught:
        client(handler).send_message("привет")
    message = str(caught.value)
    assert TOKEN not in message
    assert "sendMessage" in message
    assert "chat not found" in message


def test_transport_error_becomes_telegram_error_without_the_token() -> None:
    """httpx кладёт URL в текст своих исключений, а в URL лежит токен."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TelegramError) as caught:
        client(handler).send_message("привет")
    assert TOKEN not in str(caught.value)


def test_failure_does_not_write_the_token_to_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with caplog.at_level(logging.DEBUG), pytest.raises(TelegramError):
        client(handler).send_message("привет")
    assert TOKEN not in caplog.text


def test_invalid_url_becomes_telegram_error_without_the_token() -> None:
    """`httpx.InvalidURL` не наследует `httpx.HTTPError` (`__mro__` —
    `(InvalidURL, Exception, ...)`), поэтому перечисление `except httpx.HTTPError`
    пропускает его мимо, и он вылетает наружу вместе с URL, то есть токеном.
    Управляющий символ внутри токена правдоподобен при кривом `.env`: `.strip()`
    в `from_env` чистит только края строки."""
    broken_token = "1234567890:AAHtest\nTOKENvalueMUSTneverLEAK"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    broken_client = TelegramClient(
        TelegramCredentials(token=broken_token, chat_id=CHAT_ID), transport=transport
    )
    with pytest.raises(TelegramError) as caught:
        broken_client.send_message("привет")
    assert broken_token not in str(caught.value)
    assert "TOKENvalueMUSTneverLEAK" not in str(caught.value)


def test_ok_false_in_200_body_becomes_telegram_error() -> None:
    """Bot API отвечает статусом 200 даже на смысловой отказ — успех решает
    поле `ok` в теле, а не HTTP-статус."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    with pytest.raises(TelegramError) as caught:
        client(handler).send_message("привет")
    message = str(caught.value)
    assert TOKEN not in message
    assert "chat not found" in message
