"""Приёмник `telegram`: транспорт Bot API и отправка отчёта.

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
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import (
    VACANCY_HREF_RE,
    document_header,
    escape_html,
    render_section,
)

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
        except Exception as error:  # noqa: BLE001 — см. докстринг модуля: не
            # только `httpx.HTTPError`, но и `httpx.InvalidURL` (отдельная
            # ветка наследования от `Exception`) обязаны быть перехвачены,
            # а перечисление конкретных подклассов `httpx` ненадёжно на
            # будущее. `error` МОЖЕТ содержать URL, то есть токен — наружу
            # уходит только тип исключения.
            raise TelegramError(f"{method}: транспорт отказал ({type(error).__name__})") from None
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


class TelegramSink:
    """Отчёт в приватный канал: «Топ» сообщением, файл дня документом.

    Дедупликация — по файлу дня, тем же приёмом, что у `csv` и `markdown`:
    доставка сюда at-least-once по построению, потому что при отказе ЛЮБОГО
    приёмника `report()` не помечает вакансии отправленными и они приезжают
    снова.

    Порядок: СПЕРВА отправка, ПОТОМ запись файла. Обратный порядок при этой
    дедупликации означал бы тихую потерю — см. спеку §5.
    """

    name = "telegram"

    def __init__(self, reports_dir: Path, threshold: float, client: TelegramClient) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold
        self._client = client

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        if not vacancies:
            return 0
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.html"
        existing = self._read_day_file(path)
        already = set(VACANCY_HREF_RE.findall(existing))
        fresh = [item for item in vacancies if item.discovered.url not in already]
        if not fresh:
            return 0

        section = render_section(fresh, now, self._threshold)
        document = (existing or document_header(now)) + section

        self._client.send_message(self._message(fresh))
        self._client.send_document(path.name, document.encode("utf-8"), f"Отчёт за {now:%Y-%m-%d}")

        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")
        return len(fresh)

    def _message(self, fresh: Sequence[ScoredVacancy]) -> str:
        """Шапка и «Топ» со ссылками, гарантированно короче `MESSAGE_LIMIT`.

        Счётчики здесь — отчёта, а не прогона: `Sink.emit` не получает
        `RunStats` и получать не должен, иначе ради одной строки текста
        пришлось бы менять интерфейс, общий с `csv` и `markdown` (спека §2).
        """
        top = sorted(
            (item for item in fresh if item.score.total >= self._threshold),
            key=lambda item: item.score.total,
            reverse=True,
        )
        head = f"<b>Новых вакансий: {len(fresh)}</b>, выше порога: {len(top)}"
        lines = [head]
        shown = 0
        for item in top:
            entry = self._entry(item)
            # Хвост объявляется честно, поэтому место под него резервируется
            # ДО того, как строка перестанет влезать.
            tail = f"\n\n…ещё {len(top) - shown} — в файле"
            if len("\n\n".join([*lines, entry])) + len(tail) > MESSAGE_LIMIT:
                break
            lines.append(entry)
            shown += 1
        if shown < len(top):
            lines.append(f"…ещё {len(top) - shown} — в файле")
        elif not top:
            lines.append("<i>ничего выше порога — подробности в файле</i>")
        return "\n\n".join(lines)

    @staticmethod
    def _entry(item: ScoredVacancy) -> str:
        discovered = item.discovered
        meta = " · ".join(
            part
            for part in (
                escape_html(discovered.company) if discovered.company else None,
                escape_html(discovered.area) if discovered.area else None,
                escape_html(discovered.salary.raw) if discovered.salary.raw else None,
            )
            if part
        )
        return (
            f'<a href="{discovered.url}">{escape_html(discovered.title)}</a> — '
            f"<b>{item.score.total:.1f}</b>\n{meta}"
        )

    def _read_day_file(self, path: Path) -> str:
        """Содержимое файла дня; пусто, если файла нет.

        Декодирование терпимое по той же причине, что в csv и markdown:
        запись, оборванная полным диском посреди кириллической буквы,
        оставляет в хвосте невалидный UTF-8, и строгий декодер ронял бы
        КАЖДЫЙ следующий прогон до смены суток.
        """
        if not path.exists():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")
