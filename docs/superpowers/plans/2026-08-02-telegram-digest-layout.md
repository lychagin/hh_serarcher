# Новый вид Telegram-сообщения — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить плоский список в сообщении Telegram на макет 1c «Находка дня»: карточка лучшего совпадения, до шести строк следом, сокращённая зарплата, человеческая дата публикации.

**Architecture:** Разметку переписывает `sinks/telegram_message.py`; две новые подписи для читателя (сокращённая зарплата, дата публикации) уезжают в `sinks/text.py` рядом с `format_work_formats`. Новых модулей не заводится, границы слоёв не двигаются. `render_message` получает третий аргумент `now` — у приёмника он уже есть.

**Tech Stack:** Python 3.12, uv, pydantic-модели домена, pytest. Сеть в тестах не участвует: приёмник тестируется на `FakeClient` из `tests/test_telegram_sink.py`.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-08-02-telegram-digest-layout-design.md`. При расхождении плана и спеки прав документ спеки.
- Комментарии, докстринги и сообщения коммитов — по-русски.
- Бюджет 150 строк кода на файл (непустые, не комментарий и не докстринг). `telegram_message.py` обязан уложиться; если перевалит — делится на рендер сообщения и рендер записи, а не получает строку-исключение.
- Ворота перед каждым коммитом: `./gate` (ruff check, ruff format --check, mypy, pytest).
- Сторож проверяется интеграционно: поведение сообщения проверяется через `TelegramSink.emit` с `FakeClient`, а не вызовом `render_message` напрямую. Чистые функции `text.py` — исключение: у них своих потребителей два, и они тестируются модульно в `tests/test_sinks.py`.
- Все строки, которые видит человек, — экранируются: заголовок, компания, регион и токен валюты пишет hh.ru.
- Ветка: `telegram-digest-layout` (уже создана, спека в ней закоммичена).

---

### Task 1: Сокращённая зарплата в `sinks/text.py`

**Files:**
- Modify: `hh_search/sinks/text.py`
- Test: `tests/test_sinks.py`

**Interfaces:**
- Consumes: `hh_search.domain.models.Salary` (поля `amount_from: int | None`, `amount_to: int | None`, `currency: str | None`).
- Produces: `format_salary_short(salary: Salary) -> str | None` — строка вида `450–600k ₽`, `от 487k ₽`, `до 600k ₽`, `900 $`, либо `None`, если ни одной суммы не разобрано. Тире в диапазоне — `–` (U+2013).

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_sinks.py`, в конец файла:

```python
def test_short_salary_prints_a_range_in_thousands() -> None:
    """Диапазон — «450–600k ₽»: суффикс один раз в конце, тире короткое."""
    salary = Salary(raw="от 450 000 до 600 000 ₽", amount_from=450000, amount_to=600000, currency="₽")
    assert format_salary_short(salary) == "450–600k ₽"


def test_short_salary_drops_the_remainder_instead_of_rounding_it_up() -> None:
    """487 500 даёт «от 487k», а не «от 488k».

    Округление вниз всегда в сторону скромности: «от 488k» обещало бы
    больше, чем написал работодатель, и обнаружилось бы это на собеседовании.
    """
    salary = Salary(raw="от 487 500 ₽", amount_from=487500, amount_to=None, currency="₽")
    assert format_salary_short(salary) == "от 487k ₽"


def test_short_salary_prints_only_the_upper_bound_when_there_is_no_lower() -> None:
    salary = Salary(raw="до 600 000 ₽", amount_from=None, amount_to=600000, currency="₽")
    assert format_salary_short(salary) == "до 600k ₽"


def test_short_salary_keeps_small_amounts_whole() -> None:
    """900 не превращается в «0k»: суффикс ставится, только если ОБЕ
    печатаемые суммы не меньше тысячи."""
    salary = Salary(raw="от 900 $", amount_from=900, amount_to=None, currency="$")
    assert format_salary_short(salary) == "от 900 $"


def test_short_salary_separates_thousands_with_a_space_when_it_prints_them_whole() -> None:
    salary = Salary(raw="от 900 до 5 000 ₽", amount_from=900, amount_to=5000, currency="₽")
    assert format_salary_short(salary) == "900–5 000 ₽"


def test_short_salary_without_currency_prints_the_amounts_alone() -> None:
    """Валюта не разобралась — суммы всё равно осмысленны."""
    salary = Salary(raw="от 450 000", amount_from=450000, amount_to=None, currency=None)
    assert format_salary_short(salary) == "от 450k"


def test_short_salary_is_none_when_no_amount_was_parsed() -> None:
    """`None`, а не «зарплата не указана»: вызывающий опускает часть
    мета-строки целиком вместе с разделителем."""
    assert format_salary_short(Salary()) is None
    assert format_salary_short(Salary(raw="по договорённости")) is None
```

Импорт `Salary` в `tests/test_sinks.py` уже есть; дописать надо только `format_salary_short` в строку `from hh_search.sinks.text import SNIPPET_LENGTH, format_work_formats` (порядок имён — алфавитный, иначе покраснеет правило `I`).

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_sinks.py -k short_salary -q`
Expected: FAIL — `ImportError: cannot import name 'format_salary_short'`.

- [ ] **Step 3: Реализовать**

В `hh_search/sinks/text.py`, после `_WORK_FORMAT_LABELS`:

```python
# Порог, с которого сумма печатается тысячами. Ниже него «900 $»
# превратилось бы в «0k $», то есть в неправду.
_THOUSAND = 1000
```

И в конец файла:

```python
def format_salary_short(salary: Salary) -> str | None:
    """Зарплата одной короткой строкой: «450–600k ₽», «от 487k ₽».

    Собирается из разобранных сумм, а не из `Salary.raw`: полная строка
    hh.ru («от 450 000 до 600 000 ₽ на руки») переносится на телефоне на
    вторую строку и рвёт мета-строку сообщения. Пометка «на руки» вместе с
    ней и теряется — она остаётся в файле дня и в CSV (решение владельца,
    §3 спеки вида сообщения).

    Тысячи ОТБРАСЫВАЮТСЯ, а не округляются: «от 487k» обещает меньше, чем
    написал работодатель, а «от 488k» обещало бы больше.

    `None` — ни одной суммы не разобрано. Тогда вызывающий код опускает
    часть строки целиком, а не пишет «зарплата не указана»: в сообщении из
    семи строк такая заглушка — семь строк шума.
    """
    low, high = salary.amount_from, salary.amount_to
    if low is None and high is None:
        return None
    thousands = all(value >= _THOUSAND for value in (low, high) if value is not None)
    suffix = "k" if thousands else ""
    currency = f" {salary.currency}" if salary.currency else ""

    def amount(value: int) -> str:
        # Неразрывный пробел здесь не нужен: строку никто не переносит по
        # словам, она уходит одним куском мета-строки.
        return str(value // _THOUSAND) if thousands else f"{value:_}".replace("_", " ")

    if low is not None and high is not None:
        return f"{amount(low)}–{amount(high)}{suffix}{currency}"
    if low is not None:
        return f"от {amount(low)}{suffix}{currency}"
    if high is not None:
        return f"до {amount(high)}{suffix}{currency}"
    return None
```

Дописать импорт в шапке `text.py`: `from hh_search.domain.models import Salary, WorkFormat`.

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_sinks.py -k short_salary -q`
Expected: PASS, 7 тестов.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add hh_search/sinks/text.py tests/test_sinks.py
git commit -m "feat: сокращённая зарплата для сообщения — 450–600k ₽ вместо строки hh.ru"
```

---

### Task 2: Человеческая дата публикации в `sinks/text.py`

**Files:**
- Modify: `hh_search/sinks/text.py`
- Test: `tests/test_sinks.py`

**Interfaces:**
- Consumes: ничего из Task 1.
- Produces:
  - `format_day(moment: date) -> str` — «30 июля» (день и месяц в родительном падеже). Принимает `date`, поэтому `datetime` подходит без приведения.
  - `format_published(published_at: datetime | None, now: datetime) -> str | None` — «опубликовано сегодня» / «опубликовано вчера» / «опубликовано 28 июля», либо `None`.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_sinks.py`:

```python
def test_format_day_prints_the_russian_month_in_genitive() -> None:
    """Не `strftime("%B")`: в образе локали нет, и он дал бы «July»."""
    assert format_day(datetime(2026, 7, 30, tzinfo=UTC)) == "30 июля"
    assert format_day(datetime(2026, 1, 1, tzinfo=UTC)) == "1 января"
    assert format_day(datetime(2026, 12, 31, tzinfo=UTC)) == "31 декабря"


def test_published_today_and_yesterday_are_named_by_words() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 30, 9, 0, tzinfo=UTC), now) == "опубликовано сегодня"
    assert format_published(datetime(2026, 7, 29, 23, 0, tzinfo=UTC), now) == "опубликовано вчера"


def test_older_publication_is_named_by_the_date() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 28, 9, 0, tzinfo=UTC), now) == "опубликовано 28 июля"


def test_publication_day_is_counted_in_the_zone_of_now() -> None:
    """Сутки считаются в зоне `now` — той же, в которой именуется файл дня.

    Вакансия, вышедшая в 01:00 МСК 30-го, по UTC вышла 29-го и назовётся
    вчерашней. Цена названа в спеке: вторая шкала суток в одном сообщении
    поставила бы «Отчёт за 2026-07-30» рядом с «опубликовано сегодня» про
    разные сутки.
    """
    moscow = timezone(timedelta(hours=3))
    now = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    published = datetime(2026, 7, 30, 1, 0, tzinfo=moscow)
    assert format_published(published, now) == "опубликовано вчера"


def test_naive_publication_date_is_dropped_instead_of_guessed() -> None:
    """Смещение hh.ru отдаёт (замер 2026-07-27, фикстура vacancy.html.gz:
    "datePosted": "2026-07-27T09:21:20.933+03:00"). Ветка нужна на случай
    смены формата: пропасть обязана одна строка, а не отправка целиком.
    """
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(datetime(2026, 7, 30, 9, 0), now) is None


def test_missing_publication_date_is_dropped() -> None:
    now = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert format_published(None, now) is None
```

Импорты в шапке `tests/test_sinks.py`: строка `from datetime import UTC, datetime` расширяется до `from datetime import UTC, datetime, timedelta, timezone`, а к импорту из `hh_search.sinks.text` дописываются `format_day, format_published` (в алфавитном порядке).

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_sinks.py -k "format_day or publi" -q`
Expected: FAIL — `ImportError: cannot import name 'format_day'`.

- [ ] **Step 3: Реализовать**

В `hh_search/sinks/text.py`, рядом с `_WORK_FORMAT_LABELS`:

```python
# Месяцы в родительном падеже: «30 июля». Не `strftime("%B")` — он берёт
# название из локали процесса, а в образе локали нет (`LANG` не задан), то
# есть в русском сообщении стояло бы «July».
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
```

И в конец файла:

```python
def format_day(moment: date) -> str:
    """«30 июля»: день и месяц, без года и без ведущего нуля."""
    return f"{moment.day} {_MONTHS[moment.month - 1]}"


def format_published(published_at: datetime | None, now: datetime) -> str | None:
    """«опубликовано сегодня» / «вчера» / «28 июля», либо `None`.

    Сутки считаются в зоне `now`, то есть в UTC: имя файла дня и
    `reported_at` считаются так же, а вторая шкала суток в одном сообщении
    поставила бы «Отчёт за 2026-07-30» рядом с «опубликовано сегодня» про
    разные сутки. Цена принята сознательно: вакансия, вышедшая в 01:00 МСК,
    в ночном отчёте называется вчерашней.

    Наивная дата (без смещения) даёт `None`, а не догадку о зоне: сравнивать
    её с aware `now` — `TypeError` посреди отправки, а выдуманная зона врала
    бы молча.
    """
    if published_at is None or published_at.tzinfo is None:
        return None
    day = published_at.astimezone(now.tzinfo).date()
    distance = (now.date() - day).days
    if distance == 0:
        return "опубликовано сегодня"
    if distance == 1:
        return "опубликовано вчера"
    return f"опубликовано {format_day(day)}"
```

Дописать импорт в шапке `text.py`: `from datetime import date, datetime`.

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_sinks.py -k "format_day or publi" -q`
Expected: PASS, 6 тестов.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add hh_search/sinks/text.py tests/test_sinks.py
git commit -m "feat: человеческая дата публикации — «опубликовано сегодня» вместо метки времени"
```

---

### Task 3: Каркас нового сообщения — шапка и карточка №1

**Files:**
- Modify: `hh_search/sinks/telegram_message.py` (переписывается целиком)
- Modify: `hh_search/sinks/telegram_sink.py` (одна строка — вызов `render_message`)
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `format_salary_short`, `format_day`, `format_published` из Task 1 и Task 2; `escape_html`, `escape_attr` из `sinks/html_report.py`; `MESSAGE_LIMIT`, `message_length` из `sinks/telegram_client.py`.
- Produces: `render_message(fresh: Sequence[ScoredVacancy], threshold: float, now: datetime) -> str`. Константы модуля, на которые ссылаются Task 4 и Task 7: `TOP_LIMIT: int = 7`, `TIER_HOT: float = 80.0`, `TIER_WARM: float = 70.0`.

Задача ставит каркас: шапка, карточка лучшего совпадения, пустой топ. Секция «ЕЩЁ», значки тира и хвост — Task 4 и Task 5; до них `_assemble` печатает только карточку.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_telegram_sink.py`:

```python
# --- новый вид сообщения (спека 2026-08-02-telegram-digest-layout-design) ---


def test_message_head_names_the_day_and_both_counts(tmp_path: Path) -> None:
    """Шапка: «30 июля · новых 2, выше порога 1».

    «новых», а не «просмотрено»: приёмник не получает `RunStats` и знает
    только то, что дописал сам.
    """
    client = FakeClient()
    sink(tmp_path, client).emit(
        [vacancy(vacancy_id="1", total=87.3), vacancy(vacancy_id="2", total=10.0)], NOW
    )
    assert "29 июля · новых <b>2</b>, выше порога <b>1</b>" in client.messages[0]


def test_best_match_is_a_card_with_a_subheader(tmp_path: Path) -> None:
    """Вакансия №1 — подзаголовок в `<code>` и цитата со ссылкой в `<b>`."""
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy(vacancy_id="1", title="Лучшая", total=87.3)], NOW)
    message = client.messages[0]
    assert "<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ · 87.3</code>" in message
    assert '<blockquote><b><a href="https://hh.ru/vacancy/1">Лучшая</a></b>' in message
    assert "</blockquote>" in message


def test_best_match_card_carries_company_area_salary_and_publication_date(
    tmp_path: Path,
) -> None:
    """Мета-строка карточки — четыре части через « · », дата последней."""
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy(vacancy_id="1", total=87.3)], NOW)
    assert "Р-Софт · Нижний Новгород · от 300k RUR · опубликовано сегодня" in client.messages[0]


def test_only_the_card_carries_the_publication_date(tmp_path: Path) -> None:
    """В семи строках подряд дата — шум; у находки дня она отвечает на
    первый вопрос: не протухла ли она."""
    client = FakeClient()
    sink(tmp_path, client).emit(
        [vacancy(vacancy_id="1", total=87.3), vacancy(vacancy_id="2", total=80.0)], NOW
    )
    assert client.messages[0].count("опубликовано") == 1


def test_card_without_company_does_not_double_the_separator(tmp_path: Path) -> None:
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy(vacancy_id="1", company=None, total=87.3)], NOW)
    assert " ·  · " not in client.messages[0]
    assert "Нижний Новгород · от 300k RUR" in client.messages[0]


def test_empty_top_keeps_the_head_and_says_where_to_look(tmp_path: Path) -> None:
    """Ничего выше порога — сообщение всё равно уходит: файл-то есть."""
    client = FakeClient()
    sink(tmp_path, client).emit([vacancy(vacancy_id="1", total=10.0)], NOW)
    message = client.messages[0]
    assert "новых <b>1</b>, выше порога <b>0</b>" in message
    assert "<i>ничего выше порога — подробности в файле</i>" in message
    assert "ЛУЧШЕЕ СОВПАДЕНИЕ" not in message
```

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_telegram_sink.py -k "message_head or best_match or empty_top or publication_date or double_the_separator" -q`
Expected: FAIL — в сообщении старая разметка («Новых вакансий: 2»).

- [ ] **Step 3: Переписать `telegram_message.py`**

Файл заменяется целиком:

```python
"""Сборка текста `sendMessage`: макет «Находка дня» (спека 2026-08-02).

Вынесено из `telegram_sink.py` отдельным модулем ради бюджета §4.3 основной
спеки: `emit()` приёмника уже занят дедупликацией по нескольким суткам,
черновиком и повторной доставкой документа. Функции здесь чистые — ни сети,
ни диска, — как и весь `html_report.py`.
"""

from collections.abc import Sequence
from datetime import datetime

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import escape_attr, escape_html
from hh_search.sinks.telegram_client import MESSAGE_LIMIT, message_length
from hh_search.sinks.text import format_day, format_published, format_salary_short

# Сколько вакансий выше порога уходит в сообщение: карточка плюс шесть
# строк. Не «сколько влезет в 4096»: смысл макета — «читается за три
# секунды», а выше порога в живом прогоне бывает и сорок штук. Остаток
# честно назван хвостом и лежит в файле дня.
TOP_LIMIT = 7
# Пороги значков. Абсолютные и взятые из макета: при `report_threshold: 60`
# шкала работает как задумана. Порог выше 80 сделал бы все значки
# одинаковыми — это видно с первого же сообщения и чинится здесь, поэтому в
# конфиг не выносится (§1 спеки вида сообщения).
TIER_HOT = 80.0
TIER_WARM = 70.0

_EMPTY_TOP = "<i>ничего выше порога — подробности в файле</i>"


def render_message(fresh: Sequence[ScoredVacancy], threshold: float, now: datetime) -> str:
    """Сообщение целиком, гарантированно короче `MESSAGE_LIMIT`.

    Счётчики здесь — отчёта, а не прогона: `Sink.emit` не получает
    `RunStats` и получать не должен, иначе ради одной строки текста
    пришлось бы менять интерфейс, общий с `csv` и `markdown` (спека §2).

    Длина подбирается перебором СВЕРХУ ВНИЗ: собирается сообщение на
    `TOP_LIMIT` записей, затем на одну меньше, и так до первого влезающего.
    Перебор, а не наращивание с резервом места под хвост, потому что и
    хвост, и подзаголовок «ЕЩЁ N» зависят от числа показанных записей —
    наращивание считало бы их до того, как оно известно. Семь сборок дешевле
    одной сетевой ошибки.
    """
    top = sorted(
        (item for item in fresh if item.score.total >= threshold),
        key=lambda item: item.score.total,
        reverse=True,
    )
    below = len(fresh) - len(top)
    head = _head(now, len(fresh), len(top))
    if not top:
        return f"{head}\n\n{_EMPTY_TOP}"
    limited = top[:TOP_LIMIT]
    for count in range(len(limited), 0, -1):
        message = _assemble(limited[:count], now, head, len(top), below)
        if message_length(message) <= MESSAGE_LIMIT:
            return message
    return _minimal_message(top[0], head, len(top), below)


def _head(now: datetime, total: int, above: int) -> str:
    return f"{format_day(now)} · новых <b>{total}</b>, выше порога <b>{above}</b>"


def _assemble(
    items: Sequence[ScoredVacancy], now: datetime, head: str, above: int, below: int
) -> str:
    """Сообщение из заданного числа записей — без проверки длины."""
    return "\n\n".join([head, _card(items[0], now)])


def _card(item: ScoredVacancy, now: datetime) -> str:
    """Карточка лучшего совпадения: подзаголовок и цитата.

    Значка тира здесь нет намеренно: его место занимает `★`, а первой
    записи значок «она лучшая» ничего не добавляет.
    """
    discovered = item.discovered
    link = f'<a href="{escape_attr(discovered.url)}">{escape_html(discovered.title)}</a>'
    meta = _meta(item, published=format_published(discovered.published_at, now))
    body = f"<b>{link}</b>\n{meta}" if meta else f"<b>{link}</b>"
    subheader = f"<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ · {item.score.total:.1f}</code>"
    return f"{subheader}\n<blockquote>{body}</blockquote>"


def _meta(item: ScoredVacancy, published: str | None = None) -> str:
    """«компания · регион · зарплата [· дата]»; пустые части не оставляют
    после себя разделителя.

    Экранируется всё, включая зарплату: токен валюты приходит из разметки
    hh.ru (`_CURRENCY_TOKEN_RE` пропускает и `<`), то есть это чужой текст,
    а не наше форматирование.
    """
    discovered = item.discovered
    salary = format_salary_short(discovered.salary)
    parts = (
        escape_html(discovered.company) if discovered.company else None,
        escape_html(discovered.area) if discovered.area else None,
        escape_html(salary) if salary else None,
        published,
    )
    return " · ".join(part for part in parts if part)


def _minimal_message(item: ScoredVacancy, head: str, above: int, below: int) -> str:
    """Запасной вариант: карточка с усечённым заголовком, без мета-строки.

    Заголовок усекается ИСХОДНЫМ текстом — двоичным поиском по длине
    префикса — а уже потом экранируется и оборачивается тегом. Обратный
    порядок (усечь готовую разметку) рвёт тег или именованную сущность
    (`&amp;` пополам) и даёт `400 can't parse entities` у Bot API вместо
    честного сообщения. Двоичный поиск корректен, потому что `escape_html`
    не укорачивает текст: экранированная длина префикса растёт вместе с
    длиной префикса.
    """
    prefix = (
        f"<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ · {item.score.total:.1f}</code>\n"
        f'<blockquote><b><a href="{escape_attr(item.discovered.url)}">'
    )
    suffix = "</a></b></blockquote>"
    title = item.discovered.title

    def fits(length: int) -> bool:
        card = prefix + escape_html(title[:length]) + suffix
        return message_length(f"{head}\n\n{card}") <= MESSAGE_LIMIT

    if not fits(0):
        # Не влезает даже пустой заголовок — показывать нечего, но молчать
        # нельзя: файл дня уже уехал, и человек обязан узнать, что в нём.
        return head
    low, high = 0, len(title)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return f"{head}\n\n{prefix}{escape_html(title[:low])}{suffix}"
```

Заметки для реализующего:

- `_assemble` и `_minimal_message` пока не используют `above` и `below` — их подключает Task 5. Линтер на это не ругается: в `pyproject.toml` выбраны `E, F, I, UP, B, BLE, SLF`, правила `ARG` среди них нет. Сигнатуры взяты сразу полными, чтобы Task 5 не переписывала места вызова.
- Строки — не длиннее 100 символов (`line-length = 100`); разметку собирайте промежуточными переменными, а не одной длинной f-строкой.

В `hh_search/sinks/telegram_sink.py` заменить вызов:

```python
            self._client.send_message(render_message(fresh, self._threshold, now))
```

- [ ] **Step 4: Прогнать новые тесты и весь файл**

Run: `uv run pytest tests/test_telegram_sink.py -q`
Expected: новые шесть тестов PASS. Старые тесты про 4096 (`test_a_single_oversized_entry_falls_back_to_a_minimal_link`, `test_minimal_fallback_survives_a_title_that_expands_five_times_on_escape`, `test_long_top_is_truncated_with_an_honest_tail`, `test_top_with_emoji_fits_the_limit_telegram_actually_counts`) обязаны остаться зелёными — если краснеют, чинится код, а не тест. Исключение: `test_long_top_is_truncated_with_an_honest_tail` ждёт «в файле» — хвост появится в Task 5, поэтому до него тест краснеет законно; пометить его `@pytest.mark.xfail(reason="хвост появляется в Task 5", strict=True)` и снять пометку в Task 5.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add hh_search/sinks/telegram_message.py hh_search/sinks/telegram_sink.py tests/test_telegram_sink.py
git commit -m "feat: карточка лучшего совпадения и новая шапка сообщения"
```

---

### Task 4: Секция «ЕЩЁ», значки тира и потолок в семь записей

**Files:**
- Modify: `hh_search/sinks/telegram_message.py`
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `TOP_LIMIT`, `TIER_HOT`, `TIER_WARM`, `_assemble`, `_meta` из Task 3.
- Produces: `_entry(item: ScoredVacancy) -> str` (две строки одной записи), `_tier(score: float) -> str`.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_telegram_sink.py`:

```python
def test_rest_of_the_top_goes_under_a_counted_subheader(tmp_path: Path) -> None:
    """«ЕЩЁ 2» цифрой: прописью потребовало бы склонения числительного."""
    client = FakeClient()
    sink(tmp_path, client).emit(
        [
            vacancy(vacancy_id="1", total=87.3),
            vacancy(vacancy_id="2", title="Вторая", total=80.0),
            vacancy(vacancy_id="3", title="Третья", total=73.0),
        ],
        NOW,
    )
    message = client.messages[0]
    assert "<code>ЕЩЁ 2</code>" in message
    assert '<b>80.0</b> 🔥 <a href="https://hh.ru/vacancy/2">Вторая</a>' in message
    assert '<b>73.0</b> ⚡ <a href="https://hh.ru/vacancy/3">Третья</a>' in message


def test_entries_of_the_rest_are_not_separated_by_a_blank_line(tmp_path: Path) -> None:
    """Пустая строка между вакансиями съедала половину экрана — из-за неё
    макет и переделывался."""
    client = FakeClient()
    sink(tmp_path, client).emit(
        [
            vacancy(vacancy_id="1", total=87.3),
            vacancy(vacancy_id="2", title="Вторая", total=80.0),
            vacancy(vacancy_id="3", title="Третья", total=73.0),
        ],
        NOW,
    )
    message = client.messages[0]
    assert "\n\n<b>73.0</b>" not in message, "между записями секции появилась пустая строка"


def test_a_single_vacancy_above_threshold_has_no_rest_section(tmp_path: Path) -> None:
    client = FakeClient()
    sink(tmp_path, client).emit(
        [vacancy(vacancy_id="1", total=87.3), vacancy(vacancy_id="2", total=10.0)], NOW
    )
    assert "ЕЩЁ" not in client.messages[0]


@pytest.mark.parametrize(
    ("total", "tier"),
    [(87.3, "🔥"), (80.0, "🔥"), (79.9, "⚡"), (70.0, "⚡"), (69.9, "▫️")],
)
def test_tier_marker_follows_the_absolute_score(tmp_path: Path, total: float, tier: str) -> None:
    """Границы 80 и 70 — из макета, включённые снизу."""
    client = FakeClient()
    sink(tmp_path, client).emit(
        [vacancy(vacancy_id="1", total=95.0), vacancy(vacancy_id="2", title="Вторая", total=total)],
        NOW,
    )
    assert f"{total:.1f}</b> {tier} " in client.messages[0]


def test_top_is_capped_at_seven_entries(tmp_path: Path) -> None:
    """Сорок вакансий выше порога дают ровно семь записей: карточку и «ЕЩЁ 6»."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index}", total=90.0 - index)
        for index in range(40)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    assert "<code>ЕЩЁ 6</code>" in message
    assert message.count('<a href="https://hh.ru/vacancy/') == 7


def test_capped_top_keeps_the_highest_scores(tmp_path: Path) -> None:
    """Режется хвост списка, а не его начало: в сообщении — лучшие семь."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index}", total=60.0 + index)
        for index in range(20)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    assert "Вакансия 19" in message
    assert "Вакансия 13" in message
    assert "Вакансия 12" not in message
```

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_telegram_sink.py -k "rest_of_the_top or blank_line or rest_section or tier_marker or capped" -q`
Expected: FAIL — в сообщении только карточка.

- [ ] **Step 3: Реализовать**

В `hh_search/sinks/telegram_message.py` заменить `_assemble` и дописать две функции:

```python
def _assemble(
    items: Sequence[ScoredVacancy], now: datetime, head: str, above: int, below: int
) -> str:
    """Сообщение из заданного числа записей — без проверки длины."""
    blocks = [head, _card(items[0], now)]
    rest = items[1:]
    if rest:
        # Записи секции склеены ОДНИМ переводом строки, а блоки — двумя:
        # пустая строка между вакансиями съедала половину экрана, из-за
        # неё макет и переделывался.
        blocks.append(f"<code>ЕЩЁ {len(rest)}</code>")
        blocks.append("\n".join(_entry(item) for item in rest))
    return "\n\n".join(blocks)


def _entry(item: ScoredVacancy) -> str:
    """Одна запись секции «ЕЩЁ»: балл со значком и мета-строка."""
    discovered = item.discovered
    link = f'<a href="{escape_attr(discovered.url)}">{escape_html(discovered.title)}</a>'
    line = f"<b>{item.score.total:.1f}</b> {_tier(item.score.total)} {link}"
    meta = _meta(item)
    return f"{line}\n{meta}" if meta else line


def _tier(score: float) -> str:
    if score >= TIER_HOT:
        return "🔥"
    if score >= TIER_WARM:
        return "⚡"
    return "▫️"
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_telegram_sink.py -q`
Expected: новые тесты PASS; кроме помеченного `xfail` из Task 3, красных нет.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add hh_search/sinks/telegram_message.py tests/test_telegram_sink.py
git commit -m "feat: секция «ЕЩЁ», значки тира и потолок в семь вакансий"
```

---

### Task 5: Хвост, который говорит правду

**Files:**
- Modify: `hh_search/sinks/telegram_message.py`
- Test: `tests/test_telegram_sink.py`

**Interfaces:**
- Consumes: `_assemble`, `_minimal_message` из Task 3.
- Produces: `_tail(hidden_above: int, below: int) -> str` — пустая строка, если прятать нечего.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_telegram_sink.py`:

```python
def test_tail_counts_only_what_is_below_the_threshold(tmp_path: Path) -> None:
    """Всё выше порога влезло — хвост про остальных."""
    client = FakeClient()
    batch = [vacancy(vacancy_id="1", total=87.3)]
    batch += [vacancy(vacancy_id=str(index), total=10.0) for index in range(2, 12)]
    sink(tmp_path, client).emit(batch, NOW)
    assert "📄 Ещё <b>10</b> ниже порога — в файле" in client.messages[0]


def test_tail_counts_only_what_did_not_fit_above_the_threshold(tmp_path: Path) -> None:
    """Ниже порога никого, но выше — больше семи."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index}", total=90.0 - index)
        for index in range(10)
    ]
    sink(tmp_path, client).emit(many, NOW)
    assert "📄 Ещё <b>3</b> выше порога — в файле" in client.messages[0]


def test_tail_counts_both_remainders_separately(tmp_path: Path) -> None:
    """Два разных остатка не складываются в одно число: «выше порога» —
    это то, что человек хотел бы увидеть, а не увидел."""
    client = FakeClient()
    batch = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index}", total=90.0 - index)
        for index in range(10)
    ]
    batch += [vacancy(vacancy_id=f"low{index}", total=10.0) for index in range(5)]
    sink(tmp_path, client).emit(batch, NOW)
    assert "📄 Ещё <b>3</b> выше порога и <b>5</b> ниже — в файле" in client.messages[0]


def test_no_tail_when_everything_new_is_in_the_message(tmp_path: Path) -> None:
    """Прятать нечего — строки нет вовсе, а не «Ещё 0»."""
    client = FakeClient()
    sink(tmp_path, client).emit(
        [vacancy(vacancy_id="1", total=87.3), vacancy(vacancy_id="2", total=80.0)], NOW
    )
    assert "в файле" not in client.messages[0]
```

Снять `xfail` с `test_long_top_is_truncated_with_an_honest_tail` (проставленный в Task 3).

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_telegram_sink.py -k "tail or honest_tail" -q`
Expected: FAIL — хвоста в сообщении нет.

- [ ] **Step 3: Реализовать**

В `hh_search/sinks/telegram_message.py` дописать `_tail` и подключить её в двух местах.

```python
def _tail(hidden_above: int, below: int) -> str:
    """Честный остаток: что не влезло и что осталось ниже порога.

    Два числа не складываются в одно: «выше порога» — это то, что человек
    хотел бы увидеть в сообщении и не увидел, а «ниже» он и так не ждал.

    Ни одна форма не содержит существительного при числе: «145 вакансий»
    потребовало бы согласования («1 вакансия», «2 вакансии»), то есть
    таблицы форм ради одного слова.
    """
    if hidden_above and below:
        return f"📄 Ещё <b>{hidden_above}</b> выше порога и <b>{below}</b> ниже — в файле"
    if hidden_above:
        return f"📄 Ещё <b>{hidden_above}</b> выше порога — в файле"
    if below:
        return f"📄 Ещё <b>{below}</b> ниже порога — в файле"
    return ""
```

В `_assemble` — последним блоком:

```python
    tail = _tail(above - len(items), below)
    if tail:
        blocks.append(tail)
    return "\n\n".join(blocks)
```

В `_minimal_message` хвост считается по числу ПОКАЗАННЫХ записей, а показана там ровно одна — либо ни одной:

```python
    tail = _tail(above - 1, below)
    trailer = f"\n\n{tail}" if tail else ""

    def fits(length: int) -> bool:
        card = prefix + escape_html(title[:length]) + suffix
        return message_length(f"{head}\n\n{card}{trailer}") <= MESSAGE_LIMIT

    if not fits(0):
        # Не влезает даже пустой заголовок: показана НИ ОДНА запись, и
        # хвост обязан назвать все, а не все минус одну.
        nothing = _tail(above, below)
        return f"{head}\n\n{nothing}" if nothing else head
    ...
    return f"{head}\n\n{prefix}{escape_html(title[:low])}{suffix}{trailer}"
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_telegram_sink.py -q`
Expected: всё зелёное, включая снятый с `xfail` `test_long_top_is_truncated_with_an_honest_tail`.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add hh_search/sinks/telegram_message.py tests/test_telegram_sink.py
git commit -m "feat: хвост сообщения различает остаток выше порога и ниже"
```

---

### Task 6: Потолок 4096 при новом макете

**Files:**
- Test: `tests/test_telegram_sink.py`
- Modify (если краснеет): `hh_search/sinks/telegram_message.py`

**Interfaces:**
- Consumes: всё из Task 3–5. Новых имён не появляется.

Потолок в семь записей не заменяет потолка по длине: заголовок пишет работодатель. Эта задача проверяет, что перебор сверху вниз действительно спасает.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_telegram_sink.py`:

```python
def test_seven_giant_titles_still_fit_the_limit(tmp_path: Path) -> None:
    """Семь записей — не гарантия длины: заголовок пишет работодатель.

    Перебор сверху вниз обязан показать столько, сколько влезает, и честно
    назвать остаток.
    """
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index} " + "длинная " * 120, total=90.0)
        for index in range(20)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    assert message_length(message) <= MESSAGE_LIMIT
    assert "выше порога — в файле" in message
    assert "<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ" in message


def test_tail_after_length_truncation_counts_the_dropped_entries(tmp_path: Path) -> None:
    """Запись, выброшенная потолком ДЛИНЫ, попадает в тот же хвост, что и
    выброшенная потолком в семь штук: для читателя это один остаток."""
    client = FakeClient()
    many = [
        vacancy(vacancy_id=str(index), title=f"Вакансия {index} " + "длинная " * 200, total=90.0)
        for index in range(9)
    ]
    sink(tmp_path, client).emit(many, NOW)
    message = client.messages[0]
    shown = message.count('<a href="https://hh.ru/vacancy/')
    assert shown < 7, "сообщение с такими заголовками не может вместить семь записей"
    assert f"📄 Ещё <b>{9 - shown}</b> выше порога — в файле" in message
```

- [ ] **Step 2: Прогнать**

Run: `uv run pytest tests/test_telegram_sink.py -k "giant_titles or length_truncation" -q`
Expected: зелено, если Task 3–5 сделаны верно. Если красно — чинится `render_message`, а не тест: молчаливое обрезание запрещено.

- [ ] **Step 3: Проверить бюджет файла**

Run:
```bash
uv run python - <<'PY'
from pathlib import Path
import ast
source = Path("hh_search/sinks/telegram_message.py").read_text(encoding="utf-8")
tree = ast.parse(source)
docstrings = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        docstrings.update(range(node.lineno, node.end_lineno + 1))
count = sum(
    1
    for number, line in enumerate(source.splitlines(), start=1)
    if line.strip() and not line.strip().startswith("#") and number not in docstrings
)
print(count)
PY
```
Expected: число не больше 150. Если больше — модуль делится на `telegram_message.py` (сборка сообщения целиком) и новый модуль на рендер одной записи, как разрешает §6 спеки; строка-исключение в §4.3 основной спеки не добавляется.

- [ ] **Step 4: Прогнать весь набор**

Run: `uv run pytest -q`
Expected: PASS целиком.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add tests/test_telegram_sink.py hh_search/sinks/telegram_message.py
git commit -m "test: потолок 4096 сторожится и при новом макете"
```

---

### Task 7: Документы и сторожа документов

**Files:**
- Modify: `README.md` (раздел «Отчёт в Telegram»)
- Modify: `docs/superpowers/specs/2026-07-29-telegram-sink-design.md` (§2 и абзац §8 про шапку)
- Test: `tests/test_spec_matches_code.py`

**Interfaces:**
- Consumes: `TOP_LIMIT`, `TIER_HOT`, `TIER_WARM` из `hh_search/sinks/telegram_message.py`.

- [ ] **Step 1: Написать падающие сторожа документов**

В конец `tests/test_spec_matches_code.py`:

```python
# --- вид сообщения: числа документа обязаны совпадать с константами кода ---
#
# Тот же класс дыры, что уже чинили `test_readme_cleanup_defaults_match_the_command`:
# число, переписанное в документ руками, расходится с кодом молча.


def test_documents_name_the_real_top_limit() -> None:
    """Потолок топа назван в спеке и README тем же числом, что в коде."""
    spec = LAYOUT_SPEC.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    assert f"не больше **{TOP_LIMIT}**" in spec, "спека называет другой потолок, чем TOP_LIMIT"
    assert f"ЕЩЁ {TOP_LIMIT - 1}" in spec, "макет в спеке разошёлся с потолком TOP_LIMIT"
    assert f"первые {TOP_LIMIT}" in readme, "README называет другой потолок, чем TOP_LIMIT"


def test_spec_names_the_real_tier_thresholds() -> None:
    """Границы значков 80/70 — из кода, а не переписанные в документ."""
    spec = LAYOUT_SPEC.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    assert f"`🔥` при {TIER_HOT:.0f} и выше" in spec
    assert f"`⚡` при {TIER_WARM:.0f}–{TIER_HOT - 0.1:.1f}" in spec
    assert f"`▫️` ниже {TIER_WARM:.0f}" in spec
    assert f"{TIER_HOT:.0f} и выше, {TIER_WARM:.0f}–{TIER_HOT - 0.1:.1f}" in readme


def test_readme_does_not_describe_the_retired_message_head() -> None:
    """«Новых вакансий: N» — прежняя шапка. Документ, переживший код,
    врёт молча."""
    assert "Новых вакансий: N" not in README.read_text(encoding="utf-8")


def test_telegram_sink_spec_points_at_the_layout_spec() -> None:
    """§2 старой спеки описывал прежнюю разметку. Два документа,
    утверждающих разное, хуже одного устаревшего."""
    text = TELEGRAM_SPEC.read_text(encoding="utf-8")
    assert "2026-08-02-telegram-digest-layout-design.md" in text
    assert "Новых вакансий: N" not in text
```

Рядом с существующей константой `TELEGRAM_SPEC` (она уже есть в этом файле, как и `ROOT` с `README`) добавить путь новой спеки:

```python
LAYOUT_SPEC = ROOT / "docs/superpowers/specs/2026-08-02-telegram-digest-layout-design.md"
```

Импорт констант кода — в шапку файла, рядом с прочими импортами из `hh_search`:

```python
from hh_search.sinks.telegram_message import TIER_HOT, TIER_WARM, TOP_LIMIT
```

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `uv run pytest tests/test_spec_matches_code.py -k "top_limit or tier_thresholds or retired_message_head or points_at_the_layout" -q`
Expected: FAIL — README и старая спека ещё описывают прежний вид.

- [ ] **Step 3: Править документы**

В `README.md`, раздел «Отчёт в Telegram», абзац про шапку заменить на:

```markdown
Сообщение устроено так: строка с датой и счётчиками, карточка лучшего
совпадения, затем остальные вакансии выше порога — первые 7 по баллу, — и
честный хвост про остаток, который лежит в файле. Значки 🔥 / ⚡ / ▫️
отмечают балл: 80 и выше, 70–79.9, ниже 70.

Счётчик шапки говорит «новых N», где N — сколько записал ЭТОТ запуск, а не
сколько вакансий появилось на hh.ru: `report --since 7d` честно назовёт новой
вакансию недельной давности, потому что для файла дня она и есть новая.
Зарплата в сообщении сокращена (`450–600k ₽`); полная строка hh.ru вместе с
пометкой «на руки» осталась в файле дня и в CSV.
```

В `docs/superpowers/specs/2026-07-29-telegram-sink-design.md`:

- в начало §2 добавить строку:

```markdown
> Разметку сообщения заменил макет «Находка дня» —
> `2026-08-02-telegram-digest-layout-design.md`. Ниже действуют только
> решения о СОСТАВЕ (что уходит в канал, `parse_mode=HTML`, потолок 4096 и
> запасной вариант при не влезающей записи); вид строк описан там.
```

- абзац §8 «Шапка сообщения при этом называет «Новых вакансий: N»…» переписать:

```markdown
Шапка сообщения при этом называет «новых N», где N — сколько записал этот
запуск: для файла дня вакансия недельной давности новая, и счётчик честен
именно в этом смысле (§2 и §2 спеки вида сообщения).
```

- [ ] **Step 4: Прогнать сторожа и весь набор**

Run: `uv run pytest tests/test_spec_matches_code.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Ворота и коммит**

```bash
./gate
git add README.md docs/superpowers/specs/2026-07-29-telegram-sink-design.md tests/test_spec_matches_code.py
git commit -m "docs: README и спека приёмника вслед за новым видом сообщения"
```

---

### Task 8: Проверка живьём

**Files:** ничего не меняется — это приёмка.

- [ ] **Step 1: Собрать образ**

```bash
docker compose build
```

- [ ] **Step 2: Отправить сообщение из накопленной базы, не тревожа hh.ru**

```bash
docker compose run --rm hh-search report --since 30d
```

Окно `30d` взято НЕ наугад: приёмник дедуплицирует по файлам дня за сегодня и двое предыдущих суток, и `--since 7d` показал бы «telegram: 0», если те же вакансии уже вписаны в файл 31 июля. Тридцать суток гарантируют вакансии, которых нет ни в одном из этих файлов.

- [ ] **Step 3: Посмотреть глазами**

Открыть канал в Telegram и сверить с макетом §2 спеки: карточка на месте, значки соответствуют баллам, зарплата сокращена, пустых строк между записями секции нет, хвост называет остаток.

- [ ] **Step 4: Записать результат в HANDOFF**

Дописать в `docs/superpowers/HANDOFF.md` состояние ветки, что проверено живьём и какого числа. Коммит:

```bash
git add docs/superpowers/HANDOFF.md
git commit -m "docs: HANDOFF на состояние ветки telegram-digest-layout"
```

---

## Самопроверка плана

**Покрытие спеки:**

| Раздел спеки | Задача |
|---|---|
| §2 шапка, карточка, разметка тегами | Task 3 |
| §2 три отступления от макета («новых», «ЕЩЁ 6», без «hh-search») | Task 3, Task 4 |
| §3 значки тира | Task 4 |
| §3 мета-строка и дата только у карточки | Task 3 |
| §3 сокращённая зарплата | Task 1 |
| §3 дата публикации, зона, наивная дата, месяцы | Task 2 |
| §4 потолок семи | Task 4 |
| §4 хвост в четырёх формах | Task 5 |
| §4 потолок 4096 и запасной вариант | Task 3 (код), Task 6 (сторожа) |
| §5 вырожденные случаи | Task 1 (нет зарплаты), Task 2 (нет даты), Task 3 (пустой топ, нет компании), Task 4 (ровно одна выше порога) |
| §6 куда ложится код, бюджет 150 строк | Task 3, проверка в Task 6 |
| §7 сторожа документов, README, старая спека | Task 7 |

Строка «вакансий нет вовсе» из §5 отдельной задачи не получила намеренно: ранний возврат `emit` при пустом списке уже сторожит `test_empty_input_touches_neither_network_nor_disk`.

**Согласованность имён:** `render_message(fresh, threshold, now)` — Task 3, зовётся из `telegram_sink.py` там же. `_assemble(items, now, head, above, below)` заведена в Task 3 с полной сигнатурой и дополняется в Task 4 и Task 5 без её изменения. `_meta(item, published=None)` — Task 3, зовётся из `_card` (Task 3) и `_entry` (Task 4). `format_salary_short`, `format_day`, `format_published` — Task 1 и Task 2, потребляются в Task 3 и Task 4. `TOP_LIMIT` / `TIER_HOT` / `TIER_WARM` — Task 3, потребляются в Task 4 и Task 7.
