"""Транспорт Telegram и приёмник: всё без сети, на подставном httpx.

Главный сторож здесь — про утечку токена. Он лежит в пути URL, а проект
логирует URL при отказах, поэтому первая же сетевая ошибка утащила бы
пароль бота в data/logs/hh.log — файл, который человек первым делом
кому-нибудь показывает.
"""

import logging
from pathlib import Path

import httpx
import pytest

from hh_search.sinks import build_sinks
from hh_search.sinks.telegram_client import (
    MESSAGE_LIMIT,
    TelegramClient,
    TelegramCredentials,
    TelegramError,
)
from hh_search.sinks.telegram_sink import TelegramSink
from tests.test_html_report import NOW, vacancy

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


class FakeClient:
    """Подставной транспорт: считает вызовы и запоминает отправленное."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.messages: list[str] = []
        self.documents: list[tuple[str, bytes, str]] = []
        self._fail_on = fail_on

    def send_message(self, text: str) -> None:
        if self._fail_on == "sendMessage":
            raise TelegramError("sendMessage: транспорт отказал (ConnectError)")
        self.messages.append(text)

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        if self._fail_on == "sendDocument":
            raise TelegramError("sendDocument: транспорт отказал (ConnectError)")
        self.documents.append((filename, content, caption))


def sink(tmp_path: Path, client: FakeClient, threshold: float = 60.0) -> TelegramSink:
    return TelegramSink(tmp_path, threshold, client)  # type: ignore[arg-type]


def test_emit_sends_message_and_document(tmp_path: Path) -> None:
    client = FakeClient()
    written = sink(tmp_path, client).emit([vacancy(total=87.3)], NOW)
    assert written == 1
    assert len(client.messages) == 1
    assert len(client.documents) == 1


def test_emit_writes_the_day_file(tmp_path: Path) -> None:
    sink(tmp_path, FakeClient()).emit([vacancy()], NOW)
    assert (tmp_path / "2026-07-29-new.html").exists()


def test_document_filename_matches_the_day_file(tmp_path: Path) -> None:
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy()], NOW)
    assert client.documents[0][0] == "2026-07-29-new.html"


def test_second_emit_of_the_same_vacancy_writes_nothing_and_stays_silent(
    tmp_path: Path,
) -> None:
    """Дедупликация по файлу дня — она же защита от дубля в канале.

    При отказе ЛЮБОГО приёмника вакансии не помечаются отправленными
    (`pipeline/reporting.py`) и приезжают в следующий прогон целиком.
    """
    client = FakeClient()
    target = sink(tmp_path, client)
    target.emit([vacancy(vacancy_id="1")], NOW)
    assert target.emit([vacancy(vacancy_id="1")], NOW) == 0
    assert len(client.messages) == 1
    assert len(client.documents) == 1


def test_second_emit_sends_only_the_new_vacancy(tmp_path: Path) -> None:
    client = FakeClient()
    target = sink(tmp_path, client)
    target.emit([vacancy(vacancy_id="1", title="Первая")], NOW)
    assert (
        target.emit(
            [vacancy(vacancy_id="1", title="Первая"), vacancy(vacancy_id="2", title="Вторая")],
            NOW,
        )
        == 1
    )
    assert "Вторая" in client.messages[1]
    assert "Первая" not in client.messages[1]


def test_empty_input_touches_neither_network_nor_disk(tmp_path: Path) -> None:
    """Иначе при interval_hours: 4 канал получал бы шесть пустых сводок в сутки."""
    client = FakeClient()
    assert sink(tmp_path, client).emit([], NOW) == 0
    assert not client.messages
    assert list(tmp_path.iterdir()) == []


def test_failed_send_message_leaves_the_day_file_untouched(tmp_path: Path) -> None:
    """`send_message` — первый шаг (спека §5). Его отказ обязан оставить файл нетронутым.

    Обратный порядок (запись раньше `send_message`) означал бы: отправка
    упала, вакансии уже в файле, следующий прогон их дедуплицирует и не
    отправит НИКОГДА.
    """
    client = FakeClient(fail_on="sendMessage")
    with pytest.raises(TelegramError):
        sink(tmp_path, client).emit([vacancy()], NOW)
    assert not (tmp_path / "2026-07-29-new.html").exists()


def test_retry_after_send_message_failure_sends_the_vacancy(tmp_path: Path) -> None:
    """Продолжение предыдущего: следующий прогон обязан довезти."""
    with pytest.raises(TelegramError):
        sink(tmp_path, FakeClient(fail_on="sendMessage")).emit([vacancy()], NOW)
    client = FakeClient()
    assert sink(tmp_path, client).emit([vacancy()], NOW) == 1
    assert len(client.messages) == 1


def test_partial_failure_on_send_document_still_writes_the_day_file(tmp_path: Path) -> None:
    """Файл дня пишется МЕЖДУ отправками, а не после обеих (спека §5, раунд 1).

    `send_message` уже ушёл, когда падает `send_document`. Если бы запись
    файла стояла после `send_document`, отказ оставлял бы файл пустым —
    следующий прогон не находил бы вакансию в файле и слал бы то же
    сообщение в канал повторно (находка ревью: дубль при частичном отказе).
    Запись между отправками лечит это: файл на месте ДО того, как второй
    вызов вообще может упасть.
    """
    client = FakeClient(fail_on="sendDocument")
    with pytest.raises(TelegramError):
        sink(tmp_path, client).emit([vacancy()], NOW)
    assert (tmp_path / "2026-07-29-new.html").exists()
    assert len(client.messages) == 1


def test_retry_after_send_document_failure_does_not_resend_the_message(tmp_path: Path) -> None:
    """Продолжение предыдущего: файл уже на месте, повторный прогон не шлёт сообщение снова.

    Потеря одна и самоизлечивающаяся: документ за неудачный прогон не
    пришёл, но файл дня накопительный, и следующий вызов `send_document`
    (из следующего прогона суток) довезёт его целиком, уже со всеми
    записями.
    """
    client = FakeClient(fail_on="sendDocument")
    with pytest.raises(TelegramError):
        sink(tmp_path, client).emit([vacancy()], NOW)
    retry_client = FakeClient()
    assert sink(tmp_path, retry_client).emit([vacancy()], NOW) == 0
    assert not retry_client.messages


def test_long_top_is_truncated_with_an_honest_tail(tmp_path: Path) -> None:
    """Молчаливое обрезание запрещено: 5 позиций укладываются, 500 — нет."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия номер {index} " + "и" * 80, total=90.0)
        for index in range(200)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    assert len(message) <= MESSAGE_LIMIT
    assert "в файле" in message


def test_message_escapes_dangerous_characters_in_the_title(tmp_path: Path) -> None:
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy(title="R&D <b>", total=90.0)], NOW)
    assert "R&amp;D &lt;b&gt;" in client.messages[0]


def test_document_carries_the_whole_day_not_just_the_new_part(tmp_path: Path) -> None:
    """Сообщение — «что нового», файл — «что есть» (спека §2)."""
    client = FakeClient()
    target = sink(tmp_path, client)
    target.emit([vacancy(vacancy_id="1", title="Утренняя")], NOW)
    target.emit([vacancy(vacancy_id="2", title="Вечерняя")], NOW)
    content = client.documents[1][1].decode()
    assert "Утренняя" in content
    assert "Вечерняя" in content


def test_build_sinks_creates_telegram_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    sinks = build_sinks(["telegram"], tmp_path, 60.0)
    assert [item.name for item in sinks] == ["telegram"]


def test_build_sinks_refuses_telegram_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Отказ на старте, до сети (спека §4): `build_sinks` зовётся из
    `_sinks()` до `start_run()`, и его ValueError уже даёт EXIT_CONFIG."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ValueError):
        build_sinks(["telegram"], tmp_path, 60.0)


def test_build_sinks_error_names_the_variables_not_their_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(ValueError) as caught:
        build_sinks(["telegram"], tmp_path, 60.0)
    assert "TELEGRAM_CHAT_ID" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_unknown_sink_still_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_sinks(["карандаш"], tmp_path, 60.0)
