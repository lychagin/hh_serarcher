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
import inspect
import re
from pathlib import Path

import yaml

from hh_search.__main__ import cleanup_command
from hh_search.config.models import LocationConfig
from hh_search.domain.models import WorkFormat
from hh_search.sinks.telegram_message import TIER_HOT, TIER_WARM, TOP_LIMIT

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


# --- §4.1 — единственное место, где это число вообще написано --------------
#
# Оно копировалось четырежды: README, §7 спеки, докстринг `LimitsConfig` и
# комментарий `config.example/app.yaml`. Второй поток discovery сдвинул
# образец с пяти листингов и девяти страниц на шесть и двенадцать — и все
# четыре копии соврали разом, а сторож был только у §4.1. Тест ниже держит
# инвариант «копий нет», а не «копии совпадают»: сверять копию с оригиналом
# дороже, чем не заводить копию.

# Обе формы, в которых число уже появлялось: «9 запросов к листингам» и
# «(5 листингов, 9 страниц)». Обычные иллюстрации («50 листингов по 20
# страниц» в README, «20 вакансий на страницу») под них не попадают — они не
# про образцовый конфиг и не протухают от его правки.
_VOLUME_CLAIM_RES = (
    re.compile(r"\d+\s+запрос\w*\s+к\s+листинг", re.IGNORECASE),
    re.compile(r"\(\d+\s+листинг\w*,\s*(?:суммарно\s+)?\d+\s+страниц", re.IGNORECASE),
)


def _texts_that_must_not_repeat_the_volume() -> list[tuple[str, str]]:
    """Пары «имя, текст», в которых копии быть не должно.

    Основная спека входит сюда тоже — с ВЫРЕЗАННОЙ §4.1: одна из четырёх
    копий жила именно в ней, в §7, и сторож, пропускающий весь файл целиком
    ради одного законного вхождения, был бы мёртв ровно там, где нужен.
    Приём тот же, что у окна вокруг совпадения в
    `test_no_module_repeats_the_retracted_claims_about_area_and_page_parameter`.
    """
    spec = SPEC.read_text(encoding="utf-8")
    guarded = spec_section("### 4.1", "### 4.2")
    texts = [
        ("спека без §4.1", spec.replace(guarded, "")),
        ("README.md", README.read_text(encoding="utf-8")),
    ]
    for path in [*sorted((ROOT / "config.example").glob("*.yaml")), *sorted(PACKAGE.rglob("*.py"))]:
        texts.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
    return texts


def test_only_the_guarded_section_states_the_discovery_volume() -> None:
    """Число запросов шага discovery не копируется за пределы §4.1."""
    for name, text in _texts_that_must_not_repeat_the_volume():
        for pattern in _VOLUME_CLAIM_RES:
            found = pattern.search(text)
            assert found is None, (
                f"{name}: снова копия объёма шага discovery ({found.group(0)!r}). "
                "Она протухает от любой правки `config.example/queries.yaml`; число "
                "живёт в §4.1 спеки, где его сторожит "
                "`test_spec_discovery_volume_matches_the_sample_config`"
            )


# --- §6: региональный штраф -----------------------------------------------
#
# §6 — единственное прозаическое описание формулы, и ветка «регион и формат
# работы» его забыла: §4.1, §4.3 и §5.1 обновились, а §6 продолжала говорить
# «penalty вычитается — каждый негативный сигнал стоит ~15 очков», не зная о
# втором слагаемом. Читатель, настраивающий веса по §6, не узнавал, что у
# оценки появился штраф за регион. Сторож заводится сразу, потому что
# закономерность этого проекта известна: разошлись ровно те разделы, которых
# не сторожил ни один тест.


def _scoring_section() -> str:
    return spec_section("## 6. Скоринг", "### 6.1")


def test_spec_scoring_states_the_region_rule_in_execution_order() -> None:
    """Три правила §6 — в том порядке, в каком их исполняет `_region_penalty`.

    Имена берутся из кода (`LocationConfig`, `WorkFormat`), а не переписываются
    руками: переименование поля обязано красить документ вместе с конфигом.
    """
    section = _scoring_section()
    rules = re.findall(r"^\d+\. (.+)$", section, re.M)
    assert len(rules) == 3, f"в §6 пропал нумерованный список правил региона: {rules}"
    home_areas, penalty_field = LocationConfig.model_fields
    assert home_areas in rules[0], "первое правило §6 обязано говорить про домашний регион"
    assert WorkFormat.REMOTE.value in rules[1], "второе правило §6 обязано говорить про REMOTE"
    assert penalty_field in rules[2], "третье правило §6 обязано называть поле штрафа"
    # §6 обещает «в том порядке, в каком оно исполняется», — значит порядок
    # сверяется с кодом. Сравниваются ПРОВЕРКИ, а не имена: `self._home_areas`
    # встречается в `__init__` выше по файлу, и по нему сторож был бы зелен
    # при любой перестановке (проверено мутацией). Сама перестановка сегодня
    # не меняет ни одного вердикта — все ветки замыкаются на `return 0.0`, —
    # поэтому её ловит только документ-сторож, а не тесты скоринга.
    source = (PACKAGE / "scoring/keyword.py").read_text(encoding="utf-8")
    assert source.index(" in self._home_areas:") < source.index("WorkFormat.REMOTE in work_formats")


def test_spec_scoring_states_both_defaults_of_the_region_penalty() -> None:
    """Оба умолчания «не штрафовать» названы: без них §6 описывает правило,
    которое штрафует вакансию за сбой разбора страницы.

    Первое — раздела `location` нет вовсе; второе — регион неизвестен либо
    множество форматов пусто. Поведение сторожат `test_scoring.py`
    (`test_profile_without_location_section_scores_as_before`,
    `test_unknown_area_is_not_penalised`, `test_unknown_format_is_not_penalised`),
    здесь сторожится то, что документ о них говорит.
    """
    section = _scoring_section()
    assert re.search(r"Раздела `location`[^.]*нет[^.]*штрафа нет", section), (
        "§6 больше не говорит, что без раздела `location` штрафа нет вовсе"
    )
    assert re.search(r"неизвестен[^.]*форматов пусто[^.]*штрафа тоже нет", section, re.S), (
        "§6 больше не говорит, что неизвестный регион и пустое множество форматов штрафа не несут"
    )


def test_spec_scoring_does_not_promise_a_separate_score_detail_entry() -> None:
    """Штраф за регион складывается в общее поле `penalty`, отдельной записи нет.

    Решение принято планом и закреплено `test_penalty_lands_in_score_detail`;
    документ, обещающий отдельную запись, обещает отчёт, которого не будет.
    """
    from hh_search.domain.models import ScoreBreakdown

    assert "penalty" in ScoreBreakdown.model_fields, "в разбивке пропало поле `penalty`"
    assert not [name for name in ScoreBreakdown.model_fields if "region" in name], (
        "в разбивке появилось отдельное поле региона — §6 надо переписать"
    )
    assert "Отдельной записи в `score_detail` региональный штраф не получает" in _scoring_section()


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
        # Освобождающее «игнориру» ищется В ОКНЕ вокруг совпадения, а не по
        # всему файлу. По файлу сторож был мёртв ровно там, ради чего заведён:
        # в `listing.py` уже стоит честное «а потому что игнорируется hh.ru»,
        # и одно это слово обезвреживало проверку на весь модуль — фраза
        # «Параметр area даёт фильтр по региону» вписывалась в тот же файл, и
        # сторож оставался зелёным (проверено мутацией). Окно ±120 символов
        # накрывает предложение целиком, но не соседний абзац.
        for match in _AREA_FILTERS_CLAIM_RE.finditer(text):
            window = text[max(0, match.start() - 120) : match.end() + 120]
            assert "игнориру" in window.lower(), (
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

    Это сторож ТЕКСТА, и одного его мало: обещание README «доставит только
    в первый раз» два раунда было ложным при зелёном наборе, потому что
    сверялось наличием подстрок. Поведение сторожат исполнением
    `test_repeating_the_report_command_delivers_only_the_first_time` и
    `test_removing_the_day_files_brings_the_full_delivery_back`
    (`tests/test_telegram_sink.py`).
    """
    from hh_search.sinks.base import LOOKBACK_DAYS
    from hh_search.sinks.telegram_sink import _SENT_SUFFIX

    section = readme_section("## Отчёт в Telegram", "## Разработка")
    assert "LOOKBACK_DAYS" in section
    assert f"{LOOKBACK_DAYS} предыдущих суток" in section
    assert "недостаточно" in section, "README больше не предупреждает, что удаления файла мало"
    assert _SENT_SUFFIX in section, "README не называет файл-отметку, которую тоже надо убрать"


# --- спека приёмника telegram: ссылки на код ------------------------------


TELEGRAM_SPEC = ROOT / "docs/superpowers/specs/2026-07-29-telegram-sink-design.md"
LAYOUT_SPEC = ROOT / "docs/superpowers/specs/2026-08-02-telegram-digest-layout-design.md"

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


# --- README/спека §13 п.5: сроки уборки по умолчанию (ревью Task 5, раунд ---
# починки 1, Important-2)
#
# 90/365/90 в README и в §13 спеки — переписанные копии умолчаний
# `cleanup_command`, и дешевле некуда сверяемые исполнением: сигнатура самой
# команды уже несёт эти числа. Ни один тест их не сверял, и правка любого
# умолчания оставила бы оба документа врущими молча — тот же класс дыры,
# что уже чинили `test_readme_lists_the_real_csv_columns` и
# `test_spec_cli_block_matches_the_real_cli` для своих утверждений.


def _cleanup_command_defaults() -> dict[str, int]:
    """Умолчания срока хранения — из САМОЙ команды, а не переписанные числа."""
    signature = inspect.signature(cleanup_command)
    return {
        name: signature.parameters[name].default
        for name in ("descriptions_days", "runs_days", "reports_days")
    }


def test_readme_cleanup_defaults_match_the_command() -> None:
    defaults = _cleanup_command_defaults()
    section = readme_section("### Уборка старых данных", "## Где смотреть результаты")
    assert f"описания {defaults['descriptions_days']} дней" in section
    assert f"журнал прогонов {defaults['runs_days']}" in section
    assert f"файлы отчётов {defaults['reports_days']} (" in section


def test_spec_cleanup_defaults_match_the_command() -> None:
    defaults = _cleanup_command_defaults()
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("5. **Ретенция данных реализована**") :]
    assert f"вакансий старше {defaults['descriptions_days']} дней" in section
    assert f"прогонов старше {defaults['runs_days']} дней" in section
    assert f"отчётов старше {defaults['reports_days']} дней" in section


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
