"""Сторожа спеки: разделы, которые обязаны сверяться, а не обещать.

Финальное ревью ветки нашло закономерность, которая дороже любой отдельной
находки: три места спеки (§5.1 DDL, §7 образцы конфигов, §8.3 блок CLI) не
разошлись с кодом ПОТОМУ ЧТО их сторожат тесты — и ровно они оказались
единственными безупречными. Всё, что разошлось, — разделы без теста: §4.3
объявляла несуществующими девять живых модулей и не знала ещё о восьми,
§4.1 ошибалась в объёме прогона в полтора раза, §8.2 приводила `compose.yaml`,
который воспроизводимо падает, §10 хранила счётчик тестов, обязанный
протухать.

Отсюда правило, по которому написан этот файл: **утверждение спеки, которое
дёшево сверяется исполнением, обязано сверяться исполнением.** А счётчик,
который не сторожит ничего и устаревает сам (число тестов), из спеки убран,
а не обновлён.

Сторожа из этого файла проверены мутацией: порча утверждения в документе
красит ровно один тест.

Родственные сторожа живут в `tests/test_config_example.py` (§5.1 DDL, §7
образцы конфигов, §8.3 блок CLI): они выросли из сверки образцов
конфигурации и остались рядом с ней.
"""

import ast
import re
from pathlib import Path

import yaml

from hh_search.domain.models import WorkFormat

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs/superpowers/specs/2026-07-27-hh-autosearch-design.md"
PACKAGE = ROOT / "hh_search"
FIXTURES = Path(__file__).parent / "fixtures"

# Ориентир §4.3 после переформулировки: считаются строки КОДА, то есть
# непустые строки, не являющиеся комментарием и не входящие в докстринг.
# Объяснение неочевидного решения в комментарии ценнее соблюдения счётчика,
# поэтому комментарии из счёта исключены сознательно.
CODE_LINE_BUDGET = 150


def spec_section(start: str, end: str) -> str:
    """Срез спеки от `start` до следующего вхождения `end` ПОСЛЕ `start`.

    `end` ищется не от начала файла: иначе `end`, встретившийся раньше
    `start` (или случайно совпавший подстрокой внутри более длинного
    заголовка выше по файлу), дал бы пустой или неверный срез. Один и тот же
    класс бага чинился в `readme_section` этого файла и в `_spec_section`
    `tests/test_config_example.py` — все три сестры держат этот инвариант
    одинаково.
    """
    text = SPEC.read_text(encoding="utf-8")
    start_index = text.index(start)
    end_index = text.index(end, start_index + len(start))
    return text[start_index:end_index]


def spec_block(section: str, language: str) -> str:
    block = re.search(rf"```{language}\n(.*?)\n```", section, re.S)
    assert block is not None, f"в разделе пропал блок ```{language}```"
    return block.group(1)


# --- §4.3: инвентарь модулей ----------------------------------------------


def _tree_entries(block: str) -> set[str]:
    """Пути из дерева §4.3. Отступ в два пробела — один уровень вложенности."""
    stack: list[str] = []
    entries: set[str] = set()
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        assert indent % 2 == 0, f"нечётный отступ в дереве §4.3: {line!r}"
        name = line.strip().split()[0]
        del stack[indent // 2 :]
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
            continue
        entries.add("/".join([*stack, name]))
    return entries


def _real_entries() -> set[str]:
    """Всё содержимое пакета, кроме пустых `__init__.py`.

    Пустой `__init__.py` — маркер пакета, а не модуль: перечислять его в
    дереве значило бы семь строк шума. Всё остальное (включая непустые
    `pipeline/__init__.py` и `sinks/__init__.py`, где живёт код) обязано
    быть в дереве.
    """
    return {
        str(path.relative_to(PACKAGE.parent))
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sql"} and path.stat().st_size > 0
    }


def test_spec_module_tree_matches_the_package() -> None:
    """§4.3 обязана называть ровно те модули, которые существуют.

    Расхождение здесь стоило дороже всех прочих: шапка и §4.3 объявляли
    девять существующих модулей несозданными («задачи 1–6 реализованы,
    дальше — конвейер»), а восьми не знали вовсе. Читатель, пришедший в
    проект по ссылке из README, получал описание половины сервиса как
    плана на будущее.
    """
    documented = _tree_entries(spec_block(spec_section("### 4.3", "## 5."), "text"))
    assert documented == _real_entries()


# --- §4.3: ориентир «≤150 строк кода» -------------------------------------


def _code_lines(path: Path) -> int:
    """Непустые строки, не являющиеся комментарием и не входящие в докстринг."""
    source = path.read_text(encoding="utf-8")
    inside_docstring: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            assert first.end_lineno is not None
            inside_docstring.update(range(first.lineno, first.end_lineno + 1))
    return sum(
        1
        for number, line in enumerate(source.splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#") and number not in inside_docstring
    )


def _documented_exceptions() -> set[str]:
    """Файлы из таблицы исключений §4.3 — первая колонка, в обратных кавычках."""
    section = spec_section("**Ориентир", "## 5.")
    return {match.group(1) for match in re.finditer(r"^\| `([^`]+)` \|", section, re.M)}


def test_spec_names_every_module_over_the_code_budget() -> None:
    """Список исключений §4.3 обязан совпадать с замером, а не с памятью.

    Прежняя редакция приводила таблицу из семи файлов с точными числами
    строк — числа устарели на следующем же коммите, потому что сторожа у
    них не было. Здесь сторожится не число (оно меняется от любой правки),
    а СПИСОК: файл, перешедший границу, обязан получить в спеке строку с
    обоснованием, а файл, ужавшийся обратно, — из таблицы уйти.
    """
    measured = {
        str(path.relative_to(PACKAGE.parent))
        for path in PACKAGE.rglob("*.py")
        if _code_lines(path) > CODE_LINE_BUDGET
    }
    assert _documented_exceptions() == measured


# --- §8.2: образец compose.yaml -------------------------------------------


def test_spec_compose_sample_matches_the_real_compose() -> None:
    """§8.2 обязана приводить тот `compose.yaml`, который лежит в репозитории.

    Расхождение было не косметическим: в образце §8.2 не было ключа
    `user:`, и запуск ровно того, что описано, воспроизводимо падал «нет
    доступа к каталогу данных /data/state» — то есть образец
    воспроизводил ровно ту ловушку, которую §8.2 разбирает тремя абзацами
    ниже. Проверено исполнением 2026-07-29: `docker compose run --rm
    hh-search init-db` на образце из спеки, каталог `./data` от uid 1000,
    контейнер от uid 10001.

    Сверка семантическая (`yaml.safe_load`), а не посимвольная:
    комментарии файла в спеку не переносятся — они объясняют выбор тому,
    кто правит файл, а спека объясняет его сама.
    """
    documented = yaml.safe_load(spec_block(spec_section("### 8.2", "### 8.3"), "yaml"))
    real = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert documented == real


# --- §4.1: объём шага discovery -------------------------------------------


def test_spec_discovery_volume_matches_the_sample_config() -> None:
    """Единственное место, где документ оценивает нагрузку на источник.

    Оно и разошлось: §4.1 говорила «3 листинга, суммарно 6 страниц — 6
    запросов и потолок 120 записей», тогда как образец §7 и
    `config.example/queries.yaml` описывают пять листингов и девять
    страниц. Ошибка в полтора раза там, где вежливость к hh.ru измеряется
    числом.
    """
    stated = re.search(
        r"\((\d+) листинг\w*, суммарно (\d+) страниц\w*\) — (\d+) запрос\w* "
        r"и потолок (\d+) записей",
        spec_section("### 4.1", "### 4.2"),
    )
    assert stated is not None, "в §4.1 пропала оценка объёма шага discovery"
    listings, pages, requests, ceiling = (int(group) for group in stated.groups())
    sample = yaml.safe_load((ROOT / "config.example/queries.yaml").read_text(encoding="utf-8"))
    assert listings == len(sample["queries"])
    assert pages == sum(query["pages"] for query in sample["queries"])
    # Одна страница — один запрос; на странице листинга ровно 20 вакансий (§3.2).
    assert requests == pages
    assert ceiling == pages * 20


# --- §10: таблица фикстур -------------------------------------------------


def test_spec_fixture_table_matches_the_fixtures_on_disk() -> None:
    """Фикстуры — живые ответы источника, и §10 объясняет назначение каждой.

    Фикстура, которой в таблице нет, выглядит мусором и удаляется первой;
    строка таблицы без файла обещает покрытие, которого нет.
    """
    section = spec_section("**Фикстуры", "**Интеграционный тест")
    documented = {match.group(1) for match in re.finditer(r"^\| `([^`]+)` \|", section, re.M)}
    assert documented == {path.name for path in FIXTURES.iterdir() if path.is_file()}


# --- мета: счётчик тестов обязан отсутствовать ----------------------------


def test_spec_does_not_count_tests() -> None:
    """Число зелёных тестов в спеке — утверждение, обязанное протухать.

    Оно устаревает от любого добавленного теста, сторожить его нечем
    (сторож был бы тем же числом во второй раз), а пользы не несёт: счёт
    тестов не говорит читателю ничего, чего не говорит `uv run pytest`.
    Прошлая редакция называла число, отставшее от фактического почти
    втрое. Поэтому счётчик убран, а не обновлён, — и этот тест не даёт
    ему вернуться.
    """
    text = SPEC.read_text(encoding="utf-8")
    assert not re.search(r"\d+\s+тест\w*\s+зелён", text), (
        "в спеке снова появился счётчик тестов: он устареет на следующем коммите, "
        "а сторожить его нечем"
    )


# --- README §«Где смотреть результаты» ------------------------------------


README = ROOT / "README.md"


def readme_section(start: str, end: str) -> str:
    """Срез README от заголовка `start` до следующего заголовка `end`.

    Два независимых требования к поиску `end`, каждое проверено мутацией:

    1. Искать НАЧИНАЯ с конца `start`, а не с начала файла: иначе общий
       маркер уровня заголовка (`"## "`, а не конкретный следующий раздел)
       находил бы самый первый `## ` во всём файле — как правило, раньше
       самого `start` — и давал пустой (или неправильный) срез вместо
       секции. Для прежних вызовов с уникальным по всему файлу `end`
       результат не меняется: там первое вхождение `end` и так лежит после
       `start`.
    2. Искать `end` ТОЛЬКО в начале строки (`re.MULTILINE`, а не голый
       `str.index`): иначе подзаголовок `### Детали` сразу после `start`
       ложно совпадал бы с `end="## "` — три `#` содержат подстроку `"## "`
       начиная со второго символа, и голый `index()` находит её посреди
       строки, а не в её начале. Безобидная вставка подзаголовка обрезала
       бы секцию до пустой и красила сторож, который её читает.
    """
    text = README.read_text(encoding="utf-8")
    start_index = text.index(start)
    tail = text[start_index + len(start) :]
    end_match = re.search(rf"^{re.escape(end)}", tail, re.MULTILINE)
    assert end_match is not None, f"конец секции {end!r} после {start!r} не найден"
    end_index = start_index + len(start) + end_match.start()
    return text[start_index:end_index]


def test_readme_names_the_real_report_files() -> None:
    """Имена файлов отчёта в README обязаны совпадать с тем, что пишут приёмники.

    Секция описывает, куда человек идёт смотреть выдачу, — то есть ровно то
    утверждение, которое ломается тихо: переименование файла оставит README
    правдоподобным, а читателя отправит в пустой каталог.

    Сверяется ТАБЛИЦА, поэтому конец секции — заголовок «Отчёт в Telegram», а
    не «Разработка»: ниже HTML-файл упомянут ещё раз, в разборе повторного
    `report --since`, и с широкой границей порча строки таблицы оставалась
    зелёной (проверено мутацией).
    """
    section = readme_section("## Где смотреть результаты", "## Отчёт в Telegram")
    for module, suffix in (("csv_sink", "csv"), ("markdown_sink", "md"), ("telegram_sink", "html")):
        source = (PACKAGE / "sinks" / f"{module}.py").read_text(encoding="utf-8")
        assert f'-new.{suffix}"' in source, f"{module} больше не пишет файл `-new.{suffix}`"
        assert f"-new.{suffix}`" in section, f"README не называет файл `-new.{suffix}`"


def test_readme_lists_the_real_csv_columns() -> None:
    """Список колонок CSV в README — копия `COLUMNS`, и копии обязаны сверяться."""
    from hh_search.sinks.csv_sink import COLUMNS

    section = readme_section("## Где смотреть результаты", "## Разработка")
    assert ";".join(COLUMNS) in section, (
        "перечень колонок в README разошёлся с `csv_sink.COLUMNS`: " + ";".join(COLUMNS)
    )


def test_readme_names_the_real_report_headings() -> None:
    """Разделы markdown-отчёта названы так же, как их пишет приёмник."""
    section = readme_section("## Где смотреть результаты", "## Разработка")
    source = (PACKAGE / "sinks" / "markdown_sink.py").read_text(encoding="utf-8")
    for heading in ("## Топ", "## Остальное"):
        assert f'"{heading}"' in source, f"markdown_sink больше не пишет раздел {heading}"
        assert f"`{heading}`" in section, f"README не называет раздел {heading}"


# --- README §«Два потока discovery» ----------------------------------------


def test_readme_explains_both_discovery_streams() -> None:
    """Раздел про два потока обязан называть настоящее имя поля и настоящее
    значение перечисления — оба берутся из кода."""
    section = readme_section("## Два потока discovery", "## ")
    assert "work_format" in section
    assert WorkFormat.REMOTE.value in section


def test_readme_lists_the_location_penalty_field() -> None:
    """Штраф за неудалённую работу вне дома обязан быть виден в README, а не
    только в спеке: это раздел `profile.yaml`, который человек правит руками."""
    section = README.read_text(encoding="utf-8")
    assert "penalty_not_remote_elsewhere" in section
    assert "home_areas" in section


# --- Раунд исправлений 1 (2026-07-30): отменённые утверждения об `area` ---
#
# Ревью Task 1 нашло, что докстринг МОДУЛЯ `sources/listing.py` утверждал
# ровно то, что задача 2026-07-30 обязана была снять: (1) параметр `area` на
# `/vacancies/{slug}` не «формально проходит и отвергается из принципа» — он
# ИГНОРИРУЕТСЯ hh.ru (замер 2026-07-30: `?area=2&page=0` с нижегородского
# хоста дал нижегородскую выдачу, 0 пересечений из 20 с петербургской;
# регион задаёт поддомен, на который редиректит IP, а не параметр пути);
# (2) форма с `&page=` — не «обход духа запрета», а сознательно разрешённое
# правило `Allow: /vacancies/*?*&page=`, никакого духа запрета вокруг него
# нет. Обе отмены зафиксированы в
# `docs/superpowers/specs/2026-07-30-region-and-work-format-design.md` §0 и
# `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md` §3.2, §3.5.

_DUKH_ZAPRETA_RE = re.compile(r"дух\w*\s+запрет", re.IGNORECASE)
_AREA_FILTERS_CLAIM_RE = re.compile(r"area[^\n]{0,80}фильтр\w*\s+по\s+регион", re.IGNORECASE)


def test_no_module_repeats_the_retracted_claims_about_area_and_page_parameter() -> None:
    """Формулировка уже возвращалась в этот проект трижды — сторож ловит её код-стороной.

    Проверка не про спеку (её уже поправили), а про `hh_search/`: именно там
    докстринг `listing.py` разошёлся с фактами и с самой спекой, которую эта
    задача исполняет.
    """
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(PACKAGE.parent)
        assert not _DUKH_ZAPRETA_RE.search(text), (
            f"{rel}: снова «обход духа запрета» — `Allow: /vacancies/*?*&page=` "
            "разрешает такие параметры сознательно, обхода тут нет (design §0, spec §3.2)"
        )
        claims_area_filters = _AREA_FILTERS_CLAIM_RE.search(text) is not None
        assert not (claims_area_filters and "игнориру" not in text.lower()), (
            f"{rel}: утверждает, что `area` фильтрует по региону — измерение 2026-07-30 "
            "показало обратное: `area` игнорируется, регион задаёт поддомен (design §0)"
        )


# --- README §«Отчёт в Telegram» --------------------------------------------


def test_readme_names_the_real_telegram_variables() -> None:
    """Имена переменных в README — копия того, что читает код.

    Переменные читает `TelegramCredentials.from_env` в `telegram_client.py`,
    а не `telegram_sink.py`: транспорт и приёмник — разные модули (Task 3).
    """
    section = readme_section("## Отчёт в Telegram", "## Разработка")
    source = (PACKAGE / "sinks" / "telegram_client.py").read_text(encoding="utf-8")
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


def test_readme_recipe_for_a_repeat_delivery_matches_the_real_lookback_window() -> None:
    """README (item 4) обязано называть НАСТОЯЩЕЕ окно дедупликации.

    Прежняя редакция советовала «уберите файл дня» — после починки item 1
    это не помогает: подавление найдёт вакансию в файле одних из
    `LOOKBACK_DAYS` предыдущих суток. Число здесь не переписывается руками,
    иначе разошлось бы на следующей же правке константы.
    """
    from hh_search.sinks.telegram_sink import LOOKBACK_DAYS

    section = readme_section("## Отчёт в Telegram", "## Разработка")
    assert "LOOKBACK_DAYS" in section
    assert f"{LOOKBACK_DAYS} предыдущих суток" in section
    assert "недостаточно" in section, "README больше не предупреждает, что удаления файла мало"


# --- спека приёмника telegram: ссылки на код ------------------------------


TELEGRAM_SPEC = ROOT / "docs/superpowers/specs/2026-07-29-telegram-sink-design.md"

# Ссылка на код номером строки: `__main__.py:458`, «на строке 55». Обе формы
# протухают молча — от любой правки выше по файлу, и без единого красного
# теста.
_LINE_REFERENCE_RE = re.compile(r"\.py:\d+|на строке \d+")


def test_telegram_spec_does_not_point_at_code_by_line_number() -> None:
    """Ссылка номером строки — утверждение, обязанное протухать.

    §8 указывала на `__main__.py:458`, §5 — на «ранний возврат на строке
    55» в `pipeline/reporting.py`. Ни то, ни другое не сторожится ничем, а
    съезжает от любой вставки выше по файлу. Ссылка по имени функции
    съезжает только вместе с переименованием — и его видно.
    """
    text = TELEGRAM_SPEC.read_text(encoding="utf-8")
    found = _LINE_REFERENCE_RE.findall(text)
    assert found == [], f"в спеке снова ссылка номером строки: {found}"


def test_telegram_spec_names_functions_that_exist() -> None:
    """Имена, которыми спека заменила номера строк, обязаны существовать."""
    text = TELEGRAM_SPEC.read_text(encoding="utf-8")
    for name, module in (
        ("report_command", PACKAGE / "__main__.py"),
        ("_complain", PACKAGE / "pipeline/reporting.py"),
    ):
        assert f"`{name}`" in text, f"спека больше не ссылается на `{name}`"
        assert f"def {name}(" in module.read_text(encoding="utf-8"), (
            f"{module.name} больше не содержит `{name}`, а спека на него ссылается"
        )
