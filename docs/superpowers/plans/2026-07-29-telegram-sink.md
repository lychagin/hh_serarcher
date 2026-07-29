# Приёмник `telegram` — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Отправлять результат прогона в приватный канал Telegram — «Топ» сообщением и полный отчёт дня HTML-файлом с кликабельными ссылками.

**Architecture:** Новый приёмник встаёт в существующую точку расширения `Sink` (`hh_search/sinks/base.py`), регистрируется в `build_sinks` под именем `telegram`. Рендер HTML — отдельная чистая функция без ввода-вывода, чтобы тестироваться без сети. Транспорт — тонкая обёртка над `httpx` поверх Bot API, отдельная от вежливого клиента `sources/http.py`. Дедупликация — по файлу дня, тем же приёмом, что у `csv` и `markdown`.

**Tech Stack:** Python 3.12, httpx (уже в зависимостях), pydantic, pytest + respx (уже в dev-зависимостях). Новых зависимостей проект не получает.

**Спека:** `docs/superpowers/specs/2026-07-29-telegram-sink-design.md`. При расхождении плана и спеки верна спека.

## Global Constraints

- Ворота, обязанные быть зелёными после КАЖДОГО коммита: `uv run pytest`, `uv run mypy --strict hh_search tests`, `uv run ruff check .`, `uv run ruff format --check .`
- `line-length = 100` (ruff), `target-version = "py312"`.
- Тесты не ходят в сеть. Маркер `network` зарезервирован за контрактными тестами против живого hh.ru; ничего из этого плана им не помечается.
- **Токен не попадает ни в логи, ни в текст исключений.** Он лежит в пути URL (`/bot<ТОКЕН>/sendMessage`). Все сообщения об ошибках строятся по имени метода.
- Секреты — только из окружения (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Ни в YAML, ни в коде, ни в тестовых фикстурах настоящих значений быть не должно.
- Отказ конфигурации — **на старте**, до сети, кодом `EXIT_CONFIG`.
- Сторожа проверяются мутацией. Харнесс в этом проекте врёт дважды, поэтому обязательны `NO_COLOR=1` и `PYTHONDONTWRITEBYTECODE=1` с чисткой `__pycache__`.
- Ветка: `telegram-sink`. Коммиты частые, по одному на задачу.

---

## Файловая структура

| Файл | Ответственность |
|---|---|
| `hh_search/sinks/html_report.py` | **создаётся.** Чистый рендер: экранирование, шапка документа, блок записей. Ввода-вывода и сети нет. |
| `hh_search/sinks/telegram_sink.py` | **создаётся.** `TelegramClient` (транспорт) и `TelegramSink` (дедупликация, порядок действий, сборка сообщения). |
| `hh_search/sinks/__init__.py` | **правится.** Регистрация имени `telegram` в `build_sinks`. |
| `.env.example` | **правится.** Две новые переменные с объяснением. |
| `README.md` | **правится.** Раздел про канал и про то, чем проверить доставку. |
| `tests/test_html_report.py` | **создаётся.** Рендер и экранирование. |
| `tests/test_telegram_sink.py` | **создаётся.** Транспорт, дедупликация, порядок, лимит 4096, утечка токена. |
| `tests/test_spec_matches_code.py` | **правится.** Сторож раздела README про Telegram. |

Разделение на два модуля не косметическое: рендер обязан тестироваться без транспорта, а транспорт — без рендера. Слитый модуль заставил бы каждый тест экранирования поднимать подставной HTTP.

---

### Task 1: Рендер HTML-отчёта

**Files:**
- Create: `hh_search/sinks/html_report.py`
- Test: `tests/test_html_report.py`

**Interfaces:**
- Consumes: `ScoredVacancy` из `hh_search.domain.models` (поля: `discovered`, `details`, `score`, `cluster`); `DiscoveredVacancy` (`id`, `url`, `title`, `company`, `area`, `salary`, `published_at`); `ScoreBreakdown.total: float`; `REPORT_DATE_FORMAT` из `hh_search.sinks.base`.
- Produces:
  - `escape_html(text: str) -> str`
  - `document_header(now: datetime) -> str`
  - `render_section(vacancies: Sequence[ScoredVacancy], now: datetime, threshold: float) -> str`
  - `VACANCY_HREF_RE: re.Pattern[str]` — извлекает уже вписанные ссылки для дедупликации.

**Почему документ дописывается, а не перезаписывается.** Файл дня накапливает
находки нескольких прогонов, как `.md` и `.csv`. Перезапись означала бы
перечитывание и разбор уже написанного HTML, а разбор HTML в этом проекте
запрещён решением §11 основной спеки. Поэтому шапка пишется один раз, а каждый
прогон дописывает `<section>`; закрывающих `</body></html>` в файле нет
намеренно — браузеры такой документ рендерят, а дописывание остаётся дешёвым.

- [ ] **Step 1: Написать падающие тесты**

```python
"""Рендер HTML-отчёта: экранирование и структура.

Тесты живут отдельно от транспорта: экранирование обязано проверяться
без единого подставного HTTP-вызова.
"""

from datetime import UTC, datetime

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.sinks.html_report import (
    VACANCY_HREF_RE,
    document_header,
    escape_html,
    render_section,
)

NOW = datetime(2026, 7, 29, 10, 15, tzinfo=UTC)


def vacancy(
    vacancy_id: str = "1",
    title: str = "Backend-разработчик",
    company: str | None = "Р-Софт",
    total: float = 80.0,
    cluster: str = "backend",
) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title=title,
            company=company,
            area="Нижний Новгород",
            salary=Salary(raw="от 300 000 ₽", amount_from=300000, amount_to=None, currency="RUR"),
            published_at=NOW,
            found_by_query="programmist",
        ),
        details=VacancyDetails(
            description="Описание вакансии",
            valid_through=None,
            published_at=NOW,
            company=company,
            area="Нижний Новгород",
            salary=Salary(raw=None, amount_from=None, amount_to=None, currency=None),
        ),
        score=ScoreBreakdown(
            title=0.0,
            stack=0.0,
            responsibilities=0.0,
            domain=0.0,
            penalty=0.0,
            total=total,
            matched={},
        ),
        cluster=cluster,
    )


def test_escape_html_neutralises_the_three_dangerous_characters() -> None:
    """`&`, `<`, `>` приходят от работодателя и обязаны терять силу.

    Оба символа встретились в живом прогоне 2026-07-29: «Руководитель R&D
    по группе соусы и кетчупы» и «Руководитель группы разработки С++».
    """
    assert escape_html("R&D <b>") == "R&amp;D &lt;b&gt;"


def test_rendered_title_does_not_leak_markup() -> None:
    """Заголовок с тегом не должен становиться тегом в отчёте."""
    section = render_section([vacancy(title="C++ <script>alert(1)</script>")], NOW, 60.0)
    assert "<script>" not in section
    assert "&lt;script&gt;" in section


def test_section_puts_high_score_above_threshold_into_top() -> None:
    section = render_section([vacancy(total=87.3)], NOW, 60.0)
    assert "Топ" in section
    assert "87.3" in section


def test_section_puts_low_score_into_rest() -> None:
    section = render_section([vacancy(total=12.0)], NOW, 60.0)
    assert "Остальное" in section
    assert "12.0" in section


def test_score_exactly_at_threshold_counts_as_top() -> None:
    """Порог включающий — то же правило, что в markdown-отчёте (спека §6.3)."""
    section = render_section([vacancy(total=60.0)], NOW, 60.0)
    top, _, rest = section.partition("Остальное")
    assert "60.0" in top
    assert "60.0" not in rest


def test_links_are_clickable_anchors() -> None:
    """Кликабельность ссылок — требование владельца, ради него выбран HTML."""
    section = render_section([vacancy(vacancy_id="135501327")], NOW, 60.0)
    assert '<a href="https://hh.ru/vacancy/135501327"' in section


def test_href_regex_finds_written_links_for_deduplication() -> None:
    section = render_section([vacancy(vacancy_id="1"), vacancy(vacancy_id="2")], NOW, 60.0)
    assert set(VACANCY_HREF_RE.findall(section)) == {
        "https://hh.ru/vacancy/1",
        "https://hh.ru/vacancy/2",
    }


def test_header_is_self_contained() -> None:
    """Файл открывают с диска телефона, часто без сети: внешних ресурсов нет."""
    header = document_header(NOW)
    assert "charset=utf-8" in header
    assert "viewport" in header
    assert "http://" not in header
    assert "https://" not in header
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_html_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hh_search.sinks.html_report'`

- [ ] **Step 3: Написать модуль**

```python
"""Рендер HTML-отчёта: чистые функции, ни ввода-вывода, ни сети.

Формат выбран владельцем вместо markdown по фактической причине: телефон не
открывает `.md` браузером, и ссылки в нём остаются текстом. Кликабельность —
требование, а не удобство (спека §0).

Документ дописывается, а не перезаписывается: файл дня накапливает находки
нескольких прогонов. Перезапись означала бы разбор уже написанного HTML, а
разбор HTML запрещён решением §11 основной спеки. Поэтому шапка пишется один
раз, дальше каждый прогон дописывает `<section>`, а закрывающих тегов в файле
нет намеренно — браузер такой документ рендерит.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from itertools import groupby

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT

# Ссылки уже вписанных вакансий — вход дедупликации приёмника. Ограничение
# на `hh.ru/vacancy/` намеренное: под регулярку не должны попадать ссылки
# из шапки или подписи, иначе дедупликация начнёт считать отправленным то,
# чего в отчёте нет.
VACANCY_HREF_RE = re.compile(r'<a href="(https://hh\.ru/vacancy/[^"]+)"')

_STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;
line-height:1.5;background:#fff;color:#111}
h1{font-size:20px} h2{font-size:17px;margin-top:24px} h3{font-size:15px;color:#555}
a{color:#0a58ca;text-decoration:none} li{margin-bottom:10px}
.meta{color:#555;font-size:13px} .score{color:#0a7d28;font-weight:600}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}
a{color:#6ea8fe} h3,.meta{color:#aaa}}
"""


def escape_html(text: str) -> str:
    """Обезвредить три символа, которыми ломается разметка.

    Именно три, а не набор `MarkdownV2`: в HTML-режиме этого достаточно, и
    ровно поэтому формат сообщения выбран HTML (спека §2). Кавычки не
    экранируются — текст никогда не попадает в значение атрибута, только в
    содержимое элемента; ссылки строятся из `url`, который собран нами.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str | None, fallback: str = "—") -> str:
    return escape_html(text) if text else fallback


def document_header(now: datetime) -> str:
    """Шапка файла дня. Пишется один раз, при создании файла."""
    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Вакансии {now:%Y-%m-%d}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>Вакансии — {now:%Y-%m-%d}</h1>\n"
    )


def _entry(item: ScoredVacancy) -> str:
    discovered = item.discovered
    published = (
        "дата неизвестна"
        if discovered.published_at is None
        else format(discovered.published_at, REPORT_DATE_FORMAT)
    )
    meta = (
        f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
        f"{_plain(discovered.salary.raw, fallback='зарплата не указана')} · {published}"
    )
    return (
        f'<li><a href="{discovered.url}">{escape_html(discovered.title)}</a> '
        f'<span class="score">{item.score.total:.1f}</span>'
        f'<br><span class="meta">{meta}</span></li>\n'
    )


def render_section(
    vacancies: Sequence[ScoredVacancy], now: datetime, threshold: float
) -> str:
    """Блок одного прогона: «Топ» по кластерам и «Остальное» списком.

    Структура повторяет markdown-отчёт сознательно: два отчёта об одном и том
    же не должны расходиться в смысле. Порог включающий (`>=`), как в §6.3
    основной спеки.
    """
    ordered = sorted(vacancies, key=lambda item: item.score.total, reverse=True)
    top = [item for item in ordered if item.score.total >= threshold]
    rest = [item for item in ordered if item.score.total < threshold]

    parts = [f"<section>\n<h2>Прогон {now:%H:%M} — Топ</h2>\n"]
    if top:
        for cluster, group in groupby(
            sorted(top, key=lambda item: item.cluster), key=lambda item: item.cluster
        ):
            parts.append(f"<h3>{escape_html(cluster)}</h3>\n<ul>\n")
            parts.extend(_entry(item) for item in group)
            parts.append("</ul>\n")
    else:
        parts.append("<p><em>ничего выше порога</em></p>\n")

    parts.append("<h2>Остальное</h2>\n")
    if rest:
        parts.append("<ul>\n")
        parts.extend(_entry(item) for item in rest)
        parts.append("</ul>\n")
    else:
        parts.append("<p><em>пусто</em></p>\n")
    parts.append("</section>\n")
    return "".join(parts)
```

- [ ] **Step 4: Запустить тесты и ворота**

Run: `NO_COLOR=1 uv run pytest tests/test_html_report.py -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 5: Коммит**

```bash
git add hh_search/sinks/html_report.py tests/test_html_report.py
git commit -m "feat(sinks): рендер HTML-отчёта с кликабельными ссылками"
```

---

### Task 2: Транспорт Bot API

**Files:**
- Create: `hh_search/sinks/telegram_sink.py` (в этой задаче — только `TelegramClient` и разрешение секретов)
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `httpx`.
- Produces:
  - `class TelegramError(RuntimeError)`
  - `class TelegramCredentials` — `token: str`, `chat_id: str`; конструктор `from_env(env: Mapping[str, str]) -> TelegramCredentials`, поднимает `ValueError` при отсутствии или пустоте переменной.
  - `class TelegramClient` — `__init__(credentials: TelegramCredentials, timeout_sec: float = 20.0, transport: httpx.BaseTransport | None = None)`, методы `send_message(text: str) -> None`, `send_document(filename: str, content: bytes, caption: str) -> None`. Параметр `transport` существует ради тестов: он позволяет подставить `httpx.MockTransport` и не поднимать сеть. В рабочем коде не передаётся никогда.
  - `MESSAGE_LIMIT: int = 4096`, `CAPTION_LIMIT: int = 1024` — потолки Bot API.

**Главное требование задачи — токен не должен утечь.** У Bot API он в ПУТИ URL. `httpx` кладёт URL в текст своих исключений (`ConnectError`, `HTTPStatusError`), поэтому наружу выпускать эти исключения нельзя: они ловятся и заменяются `TelegramError`, в тексте которой стоит имя метода, а не URL.

- [ ] **Step 1: Написать падающие тесты**

```python
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
    assert b"<h1>hi</h1>" in bytes(seen["body"])  # type: ignore[arg-type]
    assert b"2026-07-29-new.html" in bytes(seen["body"])  # type: ignore[arg-type]


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
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_telegram_sink.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hh_search.sinks.telegram_sink'`

- [ ] **Step 3: Написать транспорт**

```python
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
        self._call("sendMessage", data={"chat_id": self._credentials.chat_id,
                                        "text": text,
                                        "parse_mode": "HTML",
                                        "disable_web_page_preview": "true"})

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
            with httpx.Client(
                timeout=self._timeout_sec, transport=self._transport
            ) as http:
                response = http.post(url, data=data, files=files)
        except httpx.HTTPError as error:
            # `error` СОДЕРЖИТ URL, то есть токен. Наружу уходит только тип.
            raise TelegramError(
                f"{method}: транспорт отказал ({type(error).__name__})"
            ) from None
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
```

Обратите внимание на `from None` в `raise ... from None`: без него исходное
исключение `httpx` попадёт в цепочку `__cause__`, и его текст — вместе с
токеном — напечатает любой обработчик, показывающий traceback.

- [ ] **Step 4: Запустить тесты и ворота**

Run: `NO_COLOR=1 uv run pytest tests/test_telegram_sink.py -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 5: Коммит**

```bash
git add hh_search/sinks/telegram_sink.py tests/test_telegram_sink.py
git commit -m "feat(sinks): транспорт Bot API, токен не попадает в ошибки"
```

---

### Task 3: Приёмник — дедупликация, порядок, лимит

**Files:**
- Modify: `hh_search/sinks/telegram_sink.py` (добавляется `TelegramSink`)
- Test: `tests/test_telegram_sink.py` (дописываются тесты)

**Interfaces:**
- Consumes: `TelegramClient`, `TelegramCredentials`, `TelegramError` из Task 2; `document_header`, `render_section`, `escape_html`, `VACANCY_HREF_RE` из Task 1.
- Produces: `class TelegramSink` — `name = "telegram"`, `__init__(reports_dir: Path, threshold: float, client: TelegramClient)`, `emit(vacancies: Sequence[ScoredVacancy], now: datetime) -> int`.

**Порядок действий внутри `emit` критичен (спека §5):** сперва отправка, потом
запись файла. Дедупликация читает файл дня, поэтому при обратном порядке упавшая
отправка оставила бы вакансии уже вписанными — следующий прогон нашёл бы их в
файле, вернул 0 и не отправил бы НИКОГДА. Одна сетевая ошибка молча съедала бы
порцию вакансий.

- [ ] **Step 1: Дописать падающие тесты**

Дописываются в тот же `tests/test_telegram_sink.py`; `pytest`, `TelegramError`,
`TOKEN` и `CHAT_ID` там уже импортированы Task 2, дополняются только эти:

```python
from pathlib import Path

from hh_search.sinks.telegram_sink import MESSAGE_LIMIT, TelegramSink
from tests.test_html_report import NOW, vacancy


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
    assert target.emit(
        [vacancy(vacancy_id="1", title="Первая"), vacancy(vacancy_id="2", title="Вторая")],
        NOW,
    ) == 1
    assert "Вторая" in client.messages[1]
    assert "Первая" not in client.messages[1]


def test_empty_input_touches_neither_network_nor_disk(tmp_path: Path) -> None:
    """Иначе при interval_hours: 4 канал получал бы шесть пустых сводок в сутки."""
    client = FakeClient()
    assert sink(tmp_path, client).emit([], NOW) == 0
    assert not client.messages
    assert list(tmp_path.iterdir()) == []


def test_failed_send_leaves_the_day_file_untouched(tmp_path: Path) -> None:
    """Сперва отправка, потом запись (спека §5).

    Обратный порядок означал бы: отправка упала, вакансии уже в файле,
    следующий прогон их дедуплицирует и не отправит НИКОГДА.
    """
    client = FakeClient(fail_on="sendMessage")
    with pytest.raises(TelegramError):
        sink(tmp_path, client).emit([vacancy()], NOW)
    assert not (tmp_path / "2026-07-29-new.html").exists()


def test_retry_after_failure_sends_the_vacancy(tmp_path: Path) -> None:
    """Продолжение предыдущего: следующий прогон обязан довезти."""
    with pytest.raises(TelegramError):
        sink(tmp_path, FakeClient(fail_on="sendMessage")).emit([vacancy()], NOW)
    client = FakeClient()
    assert sink(tmp_path, client).emit([vacancy()], NOW) == 1
    assert len(client.messages) == 1


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
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_telegram_sink.py -q`
Expected: FAIL — `ImportError: cannot import name 'TelegramSink'`

- [ ] **Step 3: Дописать приёмник**

```python
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
        head = (
            f"<b>Новых вакансий: {len(fresh)}</b>, выше порога: {len(top)}"
        )
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
```

Импорты в шапке модуля дополняются:

```python
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import (
    VACANCY_HREF_RE,
    document_header,
    escape_html,
    render_section,
)
```

- [ ] **Step 4: Запустить тесты и ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 5: Проверить сторожа мутацией**

Порча обязана красить ровно один тест. Проверяются два самых дорогих свойства.

```bash
export NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

# Мутация 1: переставить отправку и запись местами — должен покраснеть
# test_failed_send_leaves_the_day_file_untouched
# Мутация 2: убрать `from None` в _call — должен покраснеть
# test_failure_does_not_write_the_token_to_the_log
```

Внесите каждую мутацию руками, запустите `uv run pytest tests/test_telegram_sink.py -q`,
убедитесь, что краснеет ровно ожидаемый тест, откатите мутацию.

- [ ] **Step 6: Коммит**

```bash
git add hh_search/sinks/telegram_sink.py tests/test_telegram_sink.py
git commit -m "feat(sinks): приёмник telegram с дедупликацией по файлу дня"
```

---

### Task 4: Регистрация в фабрике

**Files:**
- Modify: `hh_search/sinks/__init__.py`
- Test: `tests/test_telegram_sink.py` (дописываются тесты фабрики)

**Interfaces:**
- Consumes: `TelegramSink`, `TelegramCredentials`, `TelegramClient` из Task 2–3.
- Produces: `build_sinks` принимает имя `telegram`; сигнатура не меняется — секреты берутся из `os.environ` внутри фабрики.

Сигнатура `build_sinks(names, reports_dir, threshold)` остаётся прежней намеренно:
её вызывает `_sinks()` в `__main__.py`, и протаскивание секретов через неё
заставило бы менять три места ради значения, которое всё равно приходит из
окружения процесса.

- [ ] **Step 1: Написать падающие тесты**

```python
import os

from hh_search.sinks import build_sinks


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
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_telegram_sink.py -q -k build_sinks`
Expected: FAIL — `ValueError: неизвестный sink: telegram`

- [ ] **Step 3: Зарегистрировать приёмник**

В `hh_search/sinks/__init__.py`:

```python
import os

from hh_search.sinks.telegram_sink import TelegramClient, TelegramCredentials, TelegramSink

__all__ = ["CsvSink", "MarkdownSink", "Sink", "TelegramSink", "build_sinks"]


def build_sinks(names: Sequence[str], reports_dir: Path, threshold: float) -> list[Sink]:
    sinks: list[Sink] = []
    for name in names:
        if name == "csv":
            sinks.append(CsvSink(reports_dir))
        elif name == "markdown":
            sinks.append(MarkdownSink(reports_dir, threshold))
        elif name == "telegram":
            # Секреты читаются здесь, а не в конфиге: фабрика зовётся до
            # `start_run()` и до сети, поэтому недописанный `.env` роняет
            # процесс на старте — как и всякий дефект конфигурации (§4).
            credentials = TelegramCredentials.from_env(os.environ)
            sinks.append(TelegramSink(reports_dir, threshold, TelegramClient(credentials)))
        else:
            raise ValueError(f"неизвестный sink: {name}")
    return sinks
```

- [ ] **Step 4: Запустить тесты и ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 5: Коммит**

```bash
git add hh_search/sinks/__init__.py tests/test_telegram_sink.py
git commit -m "feat(sinks): регистрация telegram, отказ на старте без секретов"
```

---

### Task 5: Документация и сторожа документации

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_spec_matches_code.py`

**Interfaces:**
- Consumes: `TelegramSink.name`, `TelegramCredentials.from_env` — имена переменных берутся из кода.
- Produces: ничего для последующих задач.

В этом проекте документ без сторожащего теста протухает — разошлось ровно то,
что не сторожил никто. Поэтому раздел README сверяется тестом, а не обещанием.

- [ ] **Step 1: Написать падающие сторожа**

Дописать в `tests/test_spec_matches_code.py`:

```python
def test_readme_names_the_real_telegram_variables() -> None:
    """Имена переменных в README — копия того, что читает код."""
    section = readme_section("## Отчёт в Telegram", "## Разработка")
    source = (PACKAGE / "sinks" / "telegram_sink.py").read_text(encoding="utf-8")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert name in source, f"код больше не читает {name}"
        assert name in section, f"README не называет {name}"


def test_env_example_documents_the_telegram_variables() -> None:
    """`.env.example` — то, что человек копирует. Пропуск переменной там
    означает отказ на старте у каждого, кто пошёл по инструкции."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert name in example, f".env.example не называет {name}"


def test_readme_names_the_real_sink_name() -> None:
    from hh_search.sinks.telegram_sink import TelegramSink

    section = readme_section("## Отчёт в Telegram", "## Разработка")
    assert f"`{TelegramSink.name}`" in section
```

- [ ] **Step 2: Запустить и убедиться, что тесты падают**

Run: `NO_COLOR=1 uv run pytest tests/test_spec_matches_code.py -q -k telegram`
Expected: FAIL — `ValueError: substring not found` (раздела README ещё нет).

- [ ] **Step 3: Дописать `.env.example`**

```
# Приёмник `telegram` (необязательный). Если `telegram` указан в app.yaml в
# списке sinks, обе переменные обязаны быть заданы — иначе процесс
# отказывает на старте, до единого запроса к hh.ru.
# Токен выдаёт @BotFather при создании бота. Это пароль: кто им владеет,
# тот и есть ваш бот.
TELEGRAM_BOT_TOKEN=
# chat_id приватного канала отрицателен, вида -1001234567890. Минус —
# часть идентификатора, а не опечатка. Узнаётся так: написать в канал любое
# сообщение и открыть
# https://api.telegram.org/bot<ТОКЕН>/getUpdates — искать channel_post.chat.id
TELEGRAM_CHAT_ID=
```

- [ ] **Step 4: Дописать README**

Новый раздел, ставится ПЕРЕД `## Разработка` (после «Где смотреть результаты»):

````markdown
## Отчёт в Telegram

Приёмник `telegram` шлёт в приватный канал «Топ» сообщением и полный отчёт
дня HTML-файлом. HTML, а не markdown: телефон открывает его браузером, и
ссылки на вакансии остаются кликабельными.

Что сделать один раз:

1. [@BotFather](https://t.me/BotFather) → `/newbot` → получить токен.
2. Создать приватный канал.
3. Канал → *Управление* → *Администраторы* → **Добавить администратора** →
   найти бота → оставить право **Публикация сообщений**. Поиск в списке
   администраторов ищет по УЖЕ назначенным, поэтому сначала нажимайте
   «Добавить администратора». Если бот не находится и там — напишите ему
   `/start`, Telegram ищет среди тех, с кем у вас есть диалог.
4. Написать в канал любое сообщение и открыть
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates`; `channel_post.chat.id` —
   искомый `chat_id`, он отрицателен.
5. Вписать `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env` (файл в
   `.gitignore`).
6. Добавить `telegram` в `sinks` в `data/config/app.yaml`.

Проверить доставку, не потревожив hh.ru:

```bash
docker compose run --rm hh-search report --since 7d
```

Команда гоняет уже собранную базу через те же приёмники и в сеть к hh.ru не
ходит вовсе, поэтому повторять её можно сколько угодно.

Если хоть одна из двух переменных не задана, а `telegram` в `sinks` есть —
процесс отказывает на старте, до первого запроса. Пустой прогон в канал
ничего не шлёт: при `interval_hours: 4` иначе набегало бы шесть пустых
сводок в сутки.
````

- [ ] **Step 5: Запустить полные ворота**

Run: `NO_COLOR=1 uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check . && uv run ruff format --check .`
Expected: всё зелёное.

- [ ] **Step 6: Проверить сторожа мутацией**

```bash
export NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1
cp README.md /tmp/README.bak
sed -i 's/TELEGRAM_CHAT_ID/TELEGRAM_CHANNEL_ID/g' README.md
uv run pytest tests/test_spec_matches_code.py -q -k telegram   # обязан покраснеть
cp /tmp/README.bak README.md
uv run pytest tests/test_spec_matches_code.py -q -k telegram   # обязан позеленеть
```

- [ ] **Step 7: Коммит**

```bash
git add README.md .env.example tests/test_spec_matches_code.py
git commit -m "docs: раздел про Telegram и сторожа, сверяющие его с кодом"
```

---

## Приёмка

После Task 5 владелец делает живую проверку сам:

1. Вписать `telegram` в `sinks` в `data/config/app.yaml`.
2. `docker compose run --rm hh-search report --since 7d`
3. Убедиться, что в канал пришли сообщение и файл, а файл открывается на
   телефоне с кликабельными ссылками.

Запросов к hh.ru эта проверка не стоит.
