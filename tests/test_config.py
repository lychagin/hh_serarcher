from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.loader import load_config
from hh_search.config.models import QuerySpec

APP_YAML = """
contact_email: "me@lychagin-hh.ru"
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
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
    pages: 2
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
    assert cfg.queries.queries[0].slug == "programmist"


def test_user_agent_gets_contact_email_substituted(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.user_agent == "hh-search/0.1 (personal job search; me@lychagin-hh.ru)"


@pytest.mark.parametrize("template", ['"hh-search/{version}"', '"hh-search/{0}"'])
def test_unknown_placeholder_in_user_agent_is_a_config_error(tmp_path: Path, template: str) -> None:
    """Чужой плейсхолдер обязан быть ошибкой КОНФИГА, а не KeyError.

    `str.format` бросает `KeyError`/`IndexError`, pydantic их не
    заворачивает, а CLI ловит только `(OSError, ValueError)` — то есть
    пользователь получал голый traceback вместо строки с именем поля,
    вопреки политике §7 «опечатка роняет процесс на старте с внятным
    сообщением».
    """
    broken = APP_YAML.replace(
        'user_agent: "hh-search/0.1 (personal job search; {contact_email})"',
        f"user_agent: {template}",
    )
    with pytest.raises(ValidationError, match="плейсхолдер"):
        load_config(write_config(tmp_path, **{"app.yaml": broken}))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    broken = PROFILE_YAML + "\nreport_treshold: 70\n"  # опечатка в слове threshold
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    broken = PROFILE_YAML.replace("title: 0.40", "title: 0.90")
    with pytest.raises(ValidationError, match="sum to 1.0"):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_query_describes_a_listing_slug(tmp_path: Path) -> None:
    query = load_config(write_config(tmp_path)).queries.queries[0]
    assert (query.slug, query.cluster, query.weight, query.pages) == (
        "programmist",
        "embedded",
        9,
        2,
    )


# --- Раунд исправлений 3: опечатка в ЗНАЧЕНИИ так же опасна, как в имени ----

SOURCES = {"app.yaml": APP_YAML, "profile.yaml": PROFILE_YAML, "queries.yaml": QUERIES_YAML}
WEIGHTS_LINE = "weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}"


@pytest.mark.parametrize(
    ("file_name", "old", "new"),
    [
        # Ноль и отрицательное значение бесшумно выключают вежливость к hh.ru.
        ("app.yaml", "delay_between_requests_sec: 1.0", "delay_between_requests_sec: 0"),
        ("app.yaml", "delay_between_requests_sec: 1.0", "delay_between_requests_sec: -5"),
        # Клиент с нулевым таймаутом или нулём попыток нерабочий.
        ("app.yaml", "timeout_sec: 20", "timeout_sec: 0"),
        ("app.yaml", "max_retries: 3", "max_retries: 0"),
        # Расписание с интервалом 0 крутит прогон без пауз.
        ("app.yaml", "interval_hours: 4", "interval_hours: 0"),
        ("app.yaml", "interval_hours: 4", "interval_hours: -1"),
        # max_attempts: 0 отправляет вакансию в enrich_failed, не попытавшись.
        ("app.yaml", "max_attempts: 3", "max_attempts: 0"),
        ("app.yaml", "sinks: [csv, markdown]", "sinks: []"),
        (
            "app.yaml",
            'user_agent: "hh-search/0.1 (personal job search; {contact_email})"',
            'user_agent: ""',
        ),
        ("app.yaml", 'contact_email: "me@lychagin-hh.ru"', 'contact_email: "не почта вовсе"'),
        # saturation: 0 -> ZeroDivisionError в скоринге, уже ПОСЛЕ похода в сеть.
        (
            "profile.yaml",
            "saturation: {stack: 5, responsibilities: 3}",
            "saturation: {stack: 0, responsibilities: 3}",
        ),
        (
            "profile.yaml",
            "saturation: {stack: 5, responsibilities: 3}",
            "saturation: {stack: 5, responsibilities: 0}",
        ),
        # Отрицательный штраф превращает стоп-слово в бонус.
        ("profile.yaml", "penalty_per_signal: 15", "penalty_per_signal: -100"),
        # Верхней границы не было вовсе: опечатка в одну цифру обнуляет
        # ЛЮБУЮ вакансию с одним случайным стоп-словом, а у предела float
        # отказ прилетает ValidationError'ом уже изнутри score(), то есть
        # после похода в сеть.
        ("profile.yaml", "penalty_per_signal: 15", "penalty_per_signal: 1500"),
        ("profile.yaml", "penalty_per_signal: 15", "penalty_per_signal: 1e400"),
        ("profile.yaml", "report_threshold: 60", "report_threshold: 101"),
        ("profile.yaml", "report_threshold: 60", "report_threshold: -1"),
        # Веса: NaN проходил сквозь abs(nan - 1.0) > 1e-6, отрицательные — сквозь сумму.
        (
            "profile.yaml",
            WEIGHTS_LINE,
            "weights: {title: .nan, stack: .nan, responsibilities: .nan, domain: .nan}",
        ),
        (
            "profile.yaml",
            WEIGHTS_LINE,
            "weights: {title: 1.4, stack: -0.2, responsibilities: -0.1, domain: -0.1}",
        ),
        # Пустой сигнал компилируется в регулярку, совпадающую почти с любым текстом.
        ("profile.yaml", "stack: [yocto]", 'stack: [yocto, ""]'),
        ("profile.yaml", "stack: [yocto]", 'stack: ["   "]'),
        ("profile.yaml", "negative: [junior]", 'negative: [junior, ""]'),
        ("profile.yaml", "title_roles: [team lead]", 'title_roles: [""]'),
        # Пустой slug превращает URL листинга в общий индекс /vacancies.
        ("queries.yaml", "slug: programmist", 'slug: ""'),
        ("queries.yaml", "weight: 9", "weight: -1"),
        # pages: 0 — запрос, не скачивающий ни одной страницы; верхняя
        # граница ограничивает обстрел hh.ru опечаткой в одну цифру.
        ("queries.yaml", "pages: 2", "pages: 0"),
        ("queries.yaml", "pages: 2", "pages: 21"),
    ],
)
def test_out_of_range_value_is_rejected(tmp_path: Path, file_name: str, old: str, new: str) -> None:
    body = SOURCES[file_name]
    assert old in body, "текст подстановки разошёлся с эталонным конфигом"
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{file_name: body.replace(old, new)}))


def test_empty_queries_list_is_rejected(tmp_path: Path) -> None:
    broken = "defaults:\n  employment: full\nqueries: []\n"
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"queries.yaml": broken}))


def test_duplicate_top_level_key_is_rejected(tmp_path: Path) -> None:
    broken = APP_YAML + '\ncontact_email: "other@lychagin-hh.ru"\n'
    with pytest.raises(ValueError, match="contact_email") as excinfo:
        load_config(write_config(tmp_path, **{"app.yaml": broken}))
    assert "app.yaml" in str(excinfo.value)


def test_duplicate_nested_key_is_rejected(tmp_path: Path) -> None:
    broken = APP_YAML.replace("  timeout_sec: 20", "  timeout_sec: 20\n  timeout_sec: 999")
    with pytest.raises(ValueError, match="timeout_sec") as excinfo:
        load_config(write_config(tmp_path, **{"app.yaml": broken}))
    assert "app.yaml" in str(excinfo.value)


def test_duplicate_key_in_signals_is_rejected(tmp_path: Path) -> None:
    # Случайно продублированный `stack:` бесшумно выбрасывал половину сигналов.
    broken = PROFILE_YAML.replace("  stack: [yocto]", "  stack: [yocto]\n  stack: [kafka]")
    with pytest.raises(ValueError, match="stack"):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


TWO_QUERIES_YAML = """
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
  - slug: devops
    cluster: infra
    weight: 8
"""


def test_pages_defaults_to_one(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path, **{"queries.yaml": TWO_QUERIES_YAML}))
    assert [q.slug for q in cfg.queries.queries] == ["programmist", "devops"]
    assert all(q.pages == 1 for q in cfg.queries.queries)


# --- Раунд переезда discovery на листинг ---------------------------------


@pytest.mark.parametrize(
    ("slug", "case"),
    [
        ("programmist?area=66", "query-строка"),
        ("programmist&page=1", "склейка параметров"),
        ("programmist/../search/vacancy", "выход из сегмента пути"),
        ("programmist#anchor", "фрагмент"),
        ("%3Fprogrammist", "процентное кодирование"),
        ("prog rammist", "пробел внутри"),
        (" programmist", "пробел в начале"),
        # Раунд исправлений 6. Набор запрещённых символов ловил не всё:
        # httpx перед отправкой схлопывает dot-сегменты, поэтому slug «.»
        # превращал проверенный /vacancies/.?page=1 в ушедший в сеть
        # /vacancies?page=1, запрещённый живым `Disallow: *?*`. Остальные
        # случаи — то, что набор символов пропускал молча.
        (".", "сам себе dot-сегмент: httpx схлопнёт его в /vacancies"),
        ("..", "выход на уровень выше: httpx схлопнёт его в корень"),
        ("...", "не dot-сегмент, но и не существующий slug"),
        ("prog\xa0rammist", "неразрывный пробел"),
        ("programmist？area=66", "unicode-омоглиф вопросительного знака"),
        ("programmist∕..", "unicode-омоглиф слеша"),
        ("Programmist", "регистр: такого slug у hh.ru не существует"),
        ("-programmist", "дефис в начале"),
        ("programmist_1c", "подчёркивание"),
    ],
)
def test_slug_that_is_not_a_single_path_segment_is_rejected(
    tmp_path: Path, slug: str, case: str
) -> None:
    """Slug подставляется в `/vacancies/{slug}`, а живой robots.txt hh.ru
    запрещает правилом `Disallow: *?*` любой URL с query-строкой. Slug,
    протаскивающий в путь `?`, `&`, `/` или `#`, обходил бы не матчер
    robots (он-то такой URL поймает), а саму договорённость, ради которой
    discovery и переехал с RSS на листинг. Проверка перечисляет не
    запрещённое, а разрешённое (`^[a-z0-9][a-z0-9-]*$`): список запрещённых
    символов пропускал и dot-сегменты, и омоглифы, и регистр."""
    body = QUERIES_YAML.replace("slug: programmist", f'slug: "{slug}"')
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"queries.yaml": body}))


def test_parameters_of_the_forbidden_rss_search_are_no_longer_accepted(
    tmp_path: Path,
) -> None:
    """Поля RSS (text/area/experience/employment/schedule/period) листинг не
    поддерживает: любое из них стало бы query-строкой. Молча игнорировать
    их нельзя — пользователь остался бы с конфигом, который описывает
    фильтрацию, которой на самом деле нет."""
    body = QUERIES_YAML.replace("pages: 2", "pages: 2\n    area: [66]")
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"queries.yaml": body}))


@pytest.mark.parametrize(
    ("slug", "case"),
    [
        ("prog\rrammist", "возврат каретки"),
        ("prog\vrammist", "вертикальная табуляция"),
        ("prog\frammist", "перевод страницы"),
        ("prog\x00rammist", "нулевой байт"),
    ],
)
def test_control_character_in_slug_is_rejected(slug: str, case: str) -> None:
    """Управляющие символы проверяются на модели, а не через YAML: сам YAML
    трактует CR/LF как перенос строки и подменил бы значение, из-за чего
    тест сторожил бы разбор конфига вместо валидатора slug'а. В сети такой
    slug роняет прогон `httpx.InvalidURL` — мимо иерархии ошибок приложения
    и уже после старта."""
    with pytest.raises(ValidationError):
        QuerySpec(slug=slug, cluster="embedded")


# --- Раунд исправлений: группы синонимов и границы списков сигналов -------


def test_plain_string_signal_is_a_group_of_one(tmp_path: Path) -> None:
    """Простое написание остаётся простым написанием в YAML: старые
    профили не переписываются, но внутри всё уже группы."""
    cfg = load_config(write_config(tmp_path))
    assert cfg.profile.signals.stack == [["yocto"]]
    assert cfg.profile.negative == [["junior"]]


def test_nested_list_is_a_group_of_spellings(tmp_path: Path) -> None:
    """Написания одной технологии перечисляются вынужденно (§6.1), а
    считаться должны за один сигнал — для этого и вложенный список."""
    body = PROFILE_YAML.replace("stack: [yocto]", "stack: [[arm, arm64, armv7], yocto]")
    cfg = load_config(write_config(tmp_path, **{"profile.yaml": body}))
    assert cfg.profile.signals.stack == [["arm", "arm64", "armv7"], ["yocto"]]


@pytest.mark.parametrize(
    "line",
    [
        "title_roles: [team lead]",
        "title_tech: [backend]",
        "stack: [yocto]",
        "responsibilities: [архитектур]",
        "domain: [телеком]",
    ],
)
def test_empty_signal_list_is_rejected(tmp_path: Path, line: str) -> None:
    """Пустой список молча обнуляет компонент: `stack: []` навсегда держит
    стек на нуле (потолок 70), `title_roles: []` — заголовок на 0.5
    (потолок 80), а все пять пустых дают ровный ноль каждой вакансии и
    пустой отчёт без единой ошибки в логе. Спека §7 обещает, что опечатка
    в конфиге роняет процесс на старте."""
    field = line.split(":")[0]
    body = PROFILE_YAML.replace(line, f"{field}: []")
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": body}))


def test_empty_group_is_rejected(tmp_path: Path) -> None:
    body = PROFILE_YAML.replace("stack: [yocto]", "stack: [[], yocto]")
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": body}))


def test_empty_negative_list_is_allowed(tmp_path: Path) -> None:
    """Профиль без стоп-слов осмыслен: конвейер тогда не отсеивает ничего
    локально. Это единственный список сигналов, которому пустота к лицу."""
    body = PROFILE_YAML.replace("negative: [junior]", "negative: []")
    cfg = load_config(write_config(tmp_path, **{"profile.yaml": body}))
    assert cfg.profile.negative == []


@pytest.mark.parametrize(
    ("old", "new", "case"),
    [
        ("stack: [yocto]", "stack: [yocto, yocto]", "буквальный повтор"),
        ("stack: [yocto]", 'stack: [yocto, "YOCTO"]', "регистр"),
        ("stack: [yocto]", 'stack: [yocto, " yocto "]', "пробелы по краям"),
        ("stack: [yocto]", "stack: [[yocto, bsp], [kernel, yocto]]", "написание в двух группах"),
        ("stack: [yocto]", "stack: [[yocto, yocto], bsp]", "повтор внутри одной группы"),
        ("negative: [junior]", "negative: [junior, junior]", "повтор в стоп-словах"),
        ("negative: [junior]", 'negative: [1c, "1С"]', "кириллический омоглиф"),
        ("title_roles: [team lead]", 'title_roles: [team lead, "team  lead"]', "двойной пробел"),
    ],
)
def test_duplicate_signal_is_rejected(tmp_path: Path, old: str, new: str, case: str) -> None:
    """Дубликат накручивает насыщение: `stack: [yocto] * 5` при насыщении 5
    даёт 1.0 на описании с одним словом. Отвергается по НОРМАЛИЗОВАННОЙ
    форме — той самой, в которую сигнал компилируется, — иначе `yocto`,
    ` YOCTO ` и `Yocto` считались бы разными сигналами, будучи одной и той
    же регуляркой. Мотивация та же, что у `_UniqueKeyLoader` для ключей."""
    assert old in PROFILE_YAML, "текст подстановки разошёлся с эталонным конфигом"
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": PROFILE_YAML.replace(old, new)}))


def test_same_signal_in_two_different_fields_is_allowed(tmp_path: Path) -> None:
    """`python` законно стоит и в `title_tech`, и в `stack`: это разные
    компоненты формулы, и насыщение у них разное. Проверка уникальности
    работает внутри одного списка, а не поперёк профиля."""
    body = PROFILE_YAML.replace("title_tech: [backend]", "title_tech: [python]").replace(
        "stack: [yocto]", "stack: [python]"
    )
    cfg = load_config(write_config(tmp_path, **{"profile.yaml": body}))
    assert cfg.profile.signals.title_tech == [["python"]]
    assert cfg.profile.signals.stack == [["python"]]


def test_stray_spacing_is_stripped_from_a_signal(tmp_path: Path) -> None:
    """Пробелы по краям на матч не влияют (паттерн всё равно режется по
    словам), но уезжают в `reject_reason` и в разбивку как есть."""
    body = PROFILE_YAML.replace("negative: [junior]", 'negative: [" junior "]').replace(
        "title_roles: [team lead]", 'title_roles: ["team  lead"]'
    )
    cfg = load_config(write_config(tmp_path, **{"profile.yaml": body}))
    assert cfg.profile.negative == [["junior"]]
    assert cfg.profile.signals.title_roles == [["team lead"]]


@pytest.mark.parametrize("slug", ["programmist", "devops", "web-programmist", "1c-programmist"])
def test_slug_of_a_real_hh_listing_is_accepted(tmp_path: Path, slug: str) -> None:
    """Сужение slug не должно свалиться в противоположную крайность: живые
    курируемые листинги hh.ru — это строчные буквы, цифры и дефис."""
    body = QUERIES_YAML.replace("slug: programmist", f'slug: "{slug}"')
    cfg = load_config(write_config(tmp_path, **{"queries.yaml": body}))
    assert cfg.queries.queries[0].slug == slug


# --- Раунд исправлений 8: заглушка `contact_email` не имеет права уезжать ---
#
# Валидатор проверял только форму адреса, поэтому `your-email@example.com`
# из образца проходил насквозь: первый запуск с неотредактированным
# app.yaml завершался `ok`, отправив к hh.ru десяток запросов с
# несуществующим адресом в User-Agent и без единого предупреждения. §3.5
# требует честного контакта — это единственный способ hh.ru связаться с
# нами вместо того, чтобы забанить.


@pytest.mark.parametrize(
    "address",
    [
        # Ровно то, что лежит в config.example/app.yaml.
        "your-email@example.com",
        # RFC 2606 §3: домены второго уровня, зарезервированные под примеры.
        "me@example.com",
        "me@example.net",
        "me@example.org",
        "me@example.edu",
        "me@mail.example.com",
        # RFC 2606 §2 и RFC 6761: TLD, которые не будут делегированы никогда.
        "me@hh-search.example",
        "me@hh-search.test",
        "me@hh-search.invalid",
        "me@hh-search.localhost",
        # Регистр значения не имеет: домен регистронезависим.
        "me@Example.COM",
    ],
)
def test_undeliverable_documentation_address_is_rejected(tmp_path: Path, address: str) -> None:
    """Адрес в зарезервированном домене доставить нельзя ПО ОПРЕДЕЛЕНИЮ.

    Критерий выбран не «похоже на заглушку», а «домен зарезервирован
    стандартом»: RFC 2606 и RFC 6761 гарантируют, что эти имена не
    делегируются никому, то есть письмо туда не дойдёт ни при каких
    обстоятельствах. Значит контакт декоративен, а вежливость к источнику
    держится ровно на нём.
    """
    broken = APP_YAML.replace('contact_email: "me@lychagin-hh.ru"', f'contact_email: "{address}"')
    with pytest.raises(ValidationError, match="contact_email"):
        load_config(write_config(tmp_path, **{"app.yaml": broken}))


def test_the_message_says_what_to_do(tmp_path: Path) -> None:
    """Человеку нужен не диагноз, а действие: какой файл открыть и что вписать."""
    broken = APP_YAML.replace(
        'contact_email: "me@lychagin-hh.ru"', 'contact_email: "your-email@example.com"'
    )
    with pytest.raises(ValidationError, match="Заполните contact_email в app.yaml"):
        load_config(write_config(tmp_path, **{"app.yaml": broken}))


@pytest.mark.parametrize(
    "address",
    [
        # Настоящий адрес владельца из спеки §7.
        "serg.lychagin.usa@gmail.com",
        # Собственный домен — самый вероятный случай после публичной почты,
        # и он не имеет права спотыкаться о критерий заглушки.
        "hh@lychagin.dev",
        "example@lychagin.dev",
        "job-search@my-example.com",
        "me@examples.com",
        "me@test.ru",
        "me@sub.domain.example-company.io",
    ],
)
def test_a_real_address_still_passes(tmp_path: Path, address: str) -> None:
    """Контроль: критерий обязан ловить образец и не мешать живым адресам.

    `example@lychagin.dev` и `my-example.com` здесь не для красоты — на них
    ломается любая проверка «содержит example», которая напрашивается
    первой.
    """
    good = APP_YAML.replace('contact_email: "me@lychagin-hh.ru"', f'contact_email: "{address}"')
    config = load_config(write_config(tmp_path, **{"app.yaml": good}))
    assert config.app.user_agent.endswith(f"{address})")


# --- I2/M4: объём работы прогона ограничен, а конфиг не принимает дубликатов


def test_two_listings_with_the_same_slug_are_rejected(tmp_path: Path) -> None:
    """Один slug дважды — тихая опечатка с тремя последствиями сразу.

    hh.ru получает одни и те же страницы по два раза, `discovered`
    удваивается, а кластер достаётся описанию с большим `weight` — то
    есть человек, расписавший два разных кластера, получает один и
    узнать об этом ниоткуда не может.
    """
    duplicate = """
queries:
  - slug: programmist
    cluster: embedded
    weight: 9
    pages: 1
  - slug: programmist
    cluster: backend
    weight: 3
    pages: 1
"""
    with pytest.raises(ValidationError, match="описан дважды"):
        load_config(write_config(tmp_path, **{"queries.yaml": duplicate}))


def test_sum_of_pages_over_the_ceiling_is_a_config_error(tmp_path: Path) -> None:
    """`pages ≤ 20` у каждого листинга не ограничивает ПРОИЗВЕДЕНИЕ.

    Именно произведение и есть число запросов к hh.ru за прогон: конфиг
    из 50 листингов по 20 страниц принимался молча и означал тысячу
    страниц (и двадцать тысяч страниц вакансий следом).
    """
    listings = "queries:\n" + "".join(
        f"  - slug: slug-{index}\n    cluster: c{index}\n    pages: 20\n" for index in range(5)
    )
    with pytest.raises(ValidationError, match="запрашивают суммарно 100 страниц"):
        load_config(write_config(tmp_path, **{"queries.yaml": listings}))


def test_limits_that_do_not_fit_the_interval_are_a_config_error(tmp_path: Path) -> None:
    """Прогон длиннее интервала — это демон, работающий встык, без пауз.

    Считается по нижней границе, по одним лишь паузам вежливости: даже
    она ловит замеренный случай (5.8 ч пауз при `interval_hours: 4`).
    """
    app_yaml = APP_YAML.replace(
        "enrich:\n  max_attempts: 3\n",
        "enrich:\n  max_attempts: 3\nlimits:\n  listing_pages_per_run: 500\n"
        "  enrich_per_run: 20000\n",
    )
    with pytest.raises(ValidationError, match="не помещается в интервал"):
        load_config(write_config(tmp_path, **{"app.yaml": app_yaml}))


def test_default_limits_leave_the_normal_run_untouched(tmp_path: Path) -> None:
    """Штатный прогон делает 25 запросов — потолки на порядок выше.

    Предохранитель, задевающий нормальный режим, — это не предохранитель,
    а поломка, поэтому число названо тестом, а не комментарием.
    """
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.limits.listing_pages_per_run == 60
    assert cfg.app.limits.enrich_per_run == 200
    assert cfg.app.limits.rows_per_batch == 500
    assert cfg.queries.total_pages == 2  # образец: один листинг, две страницы
