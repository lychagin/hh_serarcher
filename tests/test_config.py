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
    assert cfg.app.user_agent == "hh-search/0.1 (personal job search; me@example.com)"


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
        ("app.yaml", 'user_agent: "hh-search/0.1 (personal job search; {contact_email})"',
         'user_agent: ""'),
        ("app.yaml", 'contact_email: "me@example.com"', 'contact_email: "не почта вовсе"'),
        # saturation: 0 -> ZeroDivisionError в скоринге, уже ПОСЛЕ похода в сеть.
        ("profile.yaml", "saturation: {stack: 5, responsibilities: 3}",
         "saturation: {stack: 0, responsibilities: 3}"),
        ("profile.yaml", "saturation: {stack: 5, responsibilities: 3}",
         "saturation: {stack: 5, responsibilities: 0}"),
        # Отрицательный штраф превращает стоп-слово в бонус.
        ("profile.yaml", "penalty_per_signal: 15", "penalty_per_signal: -100"),
        ("profile.yaml", "report_threshold: 60", "report_threshold: 101"),
        ("profile.yaml", "report_threshold: 60", "report_threshold: -1"),
        # Веса: NaN проходил сквозь abs(nan - 1.0) > 1e-6, отрицательные — сквозь сумму.
        ("profile.yaml", WEIGHTS_LINE,
         "weights: {title: .nan, stack: .nan, responsibilities: .nan, domain: .nan}"),
        ("profile.yaml", WEIGHTS_LINE,
         "weights: {title: 1.4, stack: -0.2, responsibilities: -0.1, domain: -0.1}"),
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
def test_out_of_range_value_is_rejected(
    tmp_path: Path, file_name: str, old: str, new: str
) -> None:
    body = SOURCES[file_name]
    assert old in body, "текст подстановки разошёлся с эталонным конфигом"
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{file_name: body.replace(old, new)}))


def test_empty_queries_list_is_rejected(tmp_path: Path) -> None:
    broken = "defaults:\n  employment: full\nqueries: []\n"
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"queries.yaml": broken}))


def test_duplicate_top_level_key_is_rejected(tmp_path: Path) -> None:
    broken = APP_YAML + '\ncontact_email: "other@example.com"\n'
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
    ],
)
def test_slug_that_is_not_a_single_path_segment_is_rejected(
    tmp_path: Path, slug: str, case: str
) -> None:
    """Slug подставляется в `/vacancies/{slug}`, а живой robots.txt hh.ru
    запрещает правилом `Disallow: *?*` любой URL с query-строкой. Slug,
    протаскивающий в путь `?`, `&`, `/` или `#`, обходил бы не матчер
    robots (он-то такой URL поймает), а саму договорённость, ради которой
    discovery и переехал с RSS на листинг."""
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
