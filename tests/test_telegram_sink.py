"""Транспорт Telegram и приёмник: всё без сети, на подставном httpx.

Главный сторож здесь — про утечку токена. Он лежит в пути URL, а проект
логирует URL при отказах, поэтому первая же сетевая ошибка утащила бы
пароль бота в data/logs/hh.log — файл, который человек первым делом
кому-нибудь показывает.
"""

import logging
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from hh_search.logging_setup import setup_logging
from hh_search.sinks import build_sinks
from hh_search.sinks.telegram_client import (
    MESSAGE_LIMIT,
    TelegramClient,
    TelegramCredentials,
    TelegramError,
    message_length,
)
from hh_search.sinks.telegram_sink import TelegramSink
from tests.test_html_report import NOW, vacancy

TOKEN = "1234567890:AAHtestTOKENvalueMUSTneverLEAK"
CHAT_ID = "-1001234567890"
# Вечер и утро по обе стороны полуночи UTC: ключ дедупликации — файл дня,
# и он новый каждые сутки (находка I2).
EVENING = datetime(2026, 7, 29, 23, 50, tzinfo=UTC)
NEXT_MORNING = datetime(2026, 7, 30, 3, 50, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """`setup_logging` перенастраивает КОРНЕВОЙ логгер — вернём его на место.

    Здесь этого мало: `TelegramClient` вешает на обработчики корня фильтр,
    вычищающий токен, и без снятия он переехал бы на обработчики pytest.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    filters = {handler: handler.filters[:] for handler in handlers}
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for handler, existing in filters.items():
        handler.filters[:] = existing
    for name, value in quiet.items():
        logging.getLogger(name).setLevel(value)


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


def test_successful_request_does_not_write_the_token_to_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Главный сторож §4, и прежний был зелен вакуумно.

    `httpx` пишет `HTTP Request: POST <URL> "HTTP/1.1 200 OK"` на КАЖДЫЙ
    успешный запрос, а токен лежит в пути URL. Прежний сторож гонял
    `ConnectError`, при котором этой строки не существует вовсе: httpx
    пишет её ПОСЛЕ получения ответа. Значит защита обязана держаться и на
    успехе, и при чужом уровне логирования: один вечер отладки запросов к
    hh.ru (`logging.getLogger("httpx").setLevel(logging.DEBUG)`) выносил
    пароль бота в `data/logs/hh.log` — файл, который человек первым делом
    кому-нибудь показывает.
    """
    setup_logging(tmp_path / "logs")
    logging.getLogger("httpx").setLevel(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client(handler).send_message("привет")

    log = (tmp_path / "logs" / "hh.log").read_text(encoding="utf-8")
    assert "HTTP Request" in log, "httpx больше не пишет строку запроса — сторож ослеп"
    assert TOKEN not in log
    assert TOKEN not in capsys.readouterr().out


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


# --- C1: отказ записи файла дня не имеет права оставить сообщение отправленным ---


@pytest.mark.skipif(os.getuid() == 0, reason="от root каталог только для чтения не бывает")
def test_read_only_reports_dir_sends_nothing_and_leaves_no_leftovers(tmp_path: Path) -> None:
    """Права и место на диске проверяются ДО сети, иначе дубль КАЖДЫЙ прогон.

    Воспроизведение ревью: каталог отчётов `chmod 0o500`, четыре подряд
    `emit` — четыре одинаковых сообщения в канал, ноль документов. Вакансии
    при отказе приёмника не помечаются отправленными, поэтому недоступный
    том становился бесконечным источником дубля раз в четыре часа. Запись
    во временный файл ДО `send_message` превращает это в отказ без
    единого обращения к Telegram.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    reports.chmod(0o500)
    client = FakeClient()
    try:
        for _ in range(4):
            with pytest.raises(OSError):
                sink(reports, client).emit([vacancy()], NOW)
        assert not client.messages, "сообщение ушло, а файла дня нет — это и есть дубль"
        assert not client.documents
        assert list(reports.iterdir()) == [], "в каталоге остался мусор от неудачной записи"
    finally:
        reports.chmod(0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="от root каталог только для чтения не бывает")
def test_retry_after_the_reports_dir_is_fixed_sends_the_message_once(tmp_path: Path) -> None:
    """Продолжение предыдущего: том починили — сообщение уходит РОВНО один раз."""
    reports = tmp_path / "reports"
    reports.mkdir()
    reports.chmod(0o500)
    with pytest.raises(OSError):
        sink(reports, FakeClient()).emit([vacancy()], NOW)
    reports.chmod(0o700)

    client = FakeClient()
    assert sink(reports, client).emit([vacancy()], NOW) == 1
    assert len(client.messages) == 1
    assert len(client.documents) == 1


def test_failed_send_message_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """Черновик убирается: иначе каталог отчётов зарастал бы обрывками.

    Файл дня при этом обязан остаться нетронутым — следующий прогон
    повторяет всё целиком.
    """
    client = FakeClient(fail_on="sendMessage")
    with pytest.raises(TelegramError):
        sink(tmp_path, client).emit([vacancy()], NOW)
    assert list(tmp_path.iterdir()) == []


def test_the_published_file_stays_readable_like_its_siblings(tmp_path: Path) -> None:
    """`mkstemp` даёт 0600, а README отправляет открывать файл браузером.

    Соседние `-new.md` и `-new.csv` пишутся обычным `open` и выходят 0644.
    Атомарная публикация не имеет права молча сузить права файла дня до
    «только владелец» — это тот сорт регрессии, которую замечают через
    неделю и не связывают с правкой.
    """
    sink(tmp_path, FakeClient()).emit([vacancy()], NOW)
    mode = stat.S_IMODE((tmp_path / "2026-07-29-new.html").stat().st_mode)
    assert mode == 0o644, f"файл дня опубликован с правами {mode:o}"


def test_the_published_file_is_byte_identical_to_the_sent_document(tmp_path: Path) -> None:
    """Спека §5: отправляемое и записанное — одно и то же байт в байт."""
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy()], NOW)
    assert (tmp_path / "2026-07-29-new.html").read_bytes() == client.documents[0][1]


# --- I2: дедупликация не имеет права обнуляться на границе суток UTC -------


def test_failure_before_midnight_does_not_repeat_the_message_after_it(tmp_path: Path) -> None:
    """Ключ дедупликации — файл дня, и он новый каждые сутки (находка I2).

    Прогон 23:50: `send_document` падает, сообщение уже ушло, вакансии не
    помечены. Прогон 03:50 следующих суток видел их новыми, потому что
    файла НОВОГО дня ещё нет, — и слал дословный повтор вчерашнего
    сообщения. При `interval_hours: 4` это штатный исход любого отказа в
    вечернем прогоне.
    """
    with pytest.raises(TelegramError):
        sink(tmp_path, FakeClient(fail_on="sendDocument")).emit([vacancy()], EVENING)
    client = FakeClient()
    assert sink(tmp_path, client).emit([vacancy()], NEXT_MORNING) == 0
    assert not client.messages


def test_failure_of_a_neighbour_sink_does_not_repeat_the_message_after_midnight(
    tmp_path: Path,
) -> None:
    """То же самое, когда telegram здоров, а падает СОСЕД (csv).

    `report()` не помечает вакансии отправленными при отказе ЛЮБОГО
    приёмника, поэтому здоровый telegram получает вчерашнюю пачку целиком.
    """
    assert sink(tmp_path, FakeClient()).emit([vacancy()], EVENING) == 1
    client = FakeClient()
    assert sink(tmp_path, client).emit([vacancy()], NEXT_MORNING) == 0
    assert not client.messages


def test_the_previous_day_file_does_not_hide_a_genuinely_new_vacancy(tmp_path: Path) -> None:
    """Проверка обратной стороны I2: вчерашний файл не глотает новое.

    Вакансия, отправленная вчера и ПОМЕЧЕННАЯ, сегодня в выборку не
    попадёт вовсе; непомеченная — ровно та, дубля которой мы избегаем. А
    вакансия, которой вчера не было, обязана уехать в канал.
    """
    sink(tmp_path, FakeClient()).emit([vacancy(vacancy_id="1", title="Вчерашняя")], EVENING)
    client = FakeClient()
    fresh = vacancy(vacancy_id="2", title="Сегодняшняя")
    assert sink(tmp_path, client).emit([fresh], NEXT_MORNING) == 1
    assert "Сегодняшняя" in client.messages[0]
    assert (tmp_path / "2026-07-30-new.html").exists()


def test_long_top_is_truncated_with_an_honest_tail(tmp_path: Path) -> None:
    """Молчаливое обрезание запрещено: 5 позиций укладываются, 500 — нет."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия номер {index} " + "и" * 80, total=90.0)
        for index in range(200)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    assert message_length(message) <= MESSAGE_LIMIT
    assert "в файле" in message


def test_message_length_counts_utf16_code_units_not_code_points() -> None:
    """Telegram считает длину в кодовых единицах UTF-16, а не в кодовых точках."""
    assert message_length("а") == 1
    assert message_length("🚀") == 2


def test_top_with_emoji_fits_the_limit_telegram_actually_counts(tmp_path: Path) -> None:
    """Счёт в кодовых точках занижал длину: 300 вакансий с эмодзи дают
    `len = 3994` при 5494 по счёту Telegram, и Bot API отвечает 400.

    Дальше яд с самоподдержкой: `send_message` падает — файл не
    публикуется — следующий прогон собирает то же сообщение — падает
    снова, и канал не получает НИЧЕГО.

    Длина здесь считается ВРУЧНУЮ, а не через `message_length`: сторож,
    зовущий проверяемую функцию, порчу этой функции переживает — мутация
    `message_length` до `len` красила только её собственный юнит-тест, а
    этот оставался зелёным.
    """
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title="🚀" * 40 + f" номер {index}", total=90.0)
        for index in range(300)
    ]
    sink(tmp_path, client).emit(many, NOW)
    assert len(client.messages[0].encode("utf-16-le")) // 2 <= MESSAGE_LIMIT


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
    with pytest.raises(ValueError) as caught:
        build_sinks(["карандаш"], tmp_path, 60.0)
    # «sink» латиницей давало на выходе заикание «в app.yaml неизвестный
    # приёмник: неизвестный sink: карандаш» (находка I4).
    assert "неизвестный приёмник: карандаш" == str(caught.value)
