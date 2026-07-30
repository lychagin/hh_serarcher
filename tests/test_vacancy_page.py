import gzip
import logging
from datetime import datetime
from pathlib import Path

import pytest

from hh_search.domain.models import WorkFormat
from hh_search.errors import FetchFailed
from hh_search.sources.vacancy_page import (
    SalaryBlockStats,
    extract_job_posting,
    extract_salary,
    html_to_text,
    parse_vacancy_page,
    vacancy_url,
)
from hh_search.sources.work_format import WorkFormatBlockStats, extract_work_formats

FIXTURE = Path(__file__).parent / "fixtures" / "vacancy.html.gz"
# Живая страница вакансии С УКАЗАННОЙ зарплатой. Нужна отдельно: у
# основной фикстуры зарплата не указана, и на ней невозможно отличить
# «блока нет» от «блок не разбирается».
SALARY_FIXTURE = Path(__file__).parent / "fixtures" / "vacancy_salary.html.gz"


def load_fixture() -> str:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return handle.read()


def load_salary_fixture() -> str:
    with gzip.open(SALARY_FIXTURE, "rt", encoding="utf-8") as handle:
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


# Пустое описание — не «вакансия без текста», а сломанный разбор. Записать его
# нельзя: pending_enrichment отбирает по `description IS NULL`, поэтому пустая
# строка навсегда фиксирует вакансию как обогащённую с баллом 0. Отказ обязан
# быть громким, чтобы включился штатный enrich_attempts.
@pytest.mark.parametrize(
    ("raw", "case"),
    [
        ("null", "null"),
        ('{"@value": "текст"}', "словарь"),
        ('["текст"]', "список"),
        ("123", "число"),
        ('""', "пустая строка"),
        ('"<p> </p>"', "разметка без текста"),
    ],
)
def test_parse_vacancy_page_raises_on_unusable_description(raw: str, case: str) -> None:
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        f'"description": {raw}}}</script>'
    )
    with pytest.raises(FetchFailed, match="description"):
        parse_vacancy_page(html)


def test_parse_vacancy_page_raises_when_description_key_is_absent() -> None:
    html = '<script type="application/ld+json">{"@type": "JobPosting"}</script>'
    with pytest.raises(FetchFailed, match="description"):
        parse_vacancy_page(html)


# --- Раунд переезда discovery на листинг ---------------------------------
#
# Листинг /vacancies/{slug} отдаёт только id, url и заголовок, поэтому
# страница вакансии стала ЕДИНСТВЕННЫМ источником компании, региона,
# зарплаты и даты публикации. Раньше всё это приходило из RSS.


def test_parse_vacancy_page_extracts_company_area_and_dates() -> None:
    """Живая страница: всё, что раньше давал RSS, кроме зарплаты, лежит в JSON-LD."""
    details = parse_vacancy_page(load_fixture())
    assert details.company == "Кадровый центр «ПРЕЗИДЕНТ»"
    assert details.area == "Москва"
    assert isinstance(details.published_at, datetime)
    assert isinstance(details.valid_through, datetime)
    assert details.published_at < details.valid_through


def test_parse_vacancy_page_extracts_salary_from_markup() -> None:
    """Зарплаты в JSON-LD нет: поля `baseSalary` у hh.ru не существует вовсе —
    эта подстрока не встречается на странице ни разу, даже когда зарплата
    указана. Единственный источник — разметка, и формат строки там тот же,
    что приходил из RSS, поэтому её разбирает тот же parse_salary."""
    assert "baseSalary" not in load_salary_fixture(), "поля нет, а не null"
    posting = extract_job_posting(load_salary_fixture())
    assert posting is not None and "baseSalary" not in posting
    details = parse_vacancy_page(load_salary_fixture())
    assert details.salary.amount_from == 100000
    assert details.salary.amount_to == 150000
    assert details.salary.currency == "₽"
    assert details.salary.raw is not None and details.salary.raw.startswith("от 100 000")


def test_page_without_salary_block_is_not_a_failure() -> None:
    """«Зарплата не указана» — законный ответ источника, а не поломка."""
    assert extract_salary(load_fixture()) is None
    details = parse_vacancy_page(load_fixture())
    assert details.salary.amount_from is None
    assert details.description, "остальной разбор при этом обязан работать"


def test_salary_stats_warn_when_the_attribute_seems_to_have_drifted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """По одной странице дрейф `data-qa` неотличим от «зарплата не указана».
    Виден он только в агрегате: прогон, где блок не нашёлся НИ РАЗУ, почти
    наверняка означает переименованный атрибут, а не рынок без зарплат."""
    stats = SalaryBlockStats()
    for _ in range(3):
        parse_vacancy_page(load_fixture(), stats)
    assert (stats.pages, stats.without_salary) == (3, 3)
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "vacancy-salary" in caplog.text


def test_salary_stats_stay_quiet_when_some_pages_do_have_salary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stats = SalaryBlockStats()
    parse_vacancy_page(load_fixture(), stats)
    parse_vacancy_page(load_salary_fixture(), stats)
    assert (stats.pages, stats.without_salary) == (2, 1)
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert caplog.text == "", "атрибут на месте — тревожить не о чем"


def test_extract_salary_ignores_a_renamed_attribute() -> None:
    """Сторож самой регулярки: она обязана держаться за конкретный data-qa,
    а не хватать первую попавшуюся сумму со страницы."""
    html = '<div data-qa="vacancy-compensation">от 250 000 ₽</div>'
    assert extract_salary(html) is None


# --- Раунд исправлений 6: сторож дрейфа обязан видеть перестройку блока ----
#
# `_SALARY_BLOCK_RE` берёт содержимое до ПЕРВОГО `</div>`. Стоит hh.ru
# обернуть сумму ещё одним `<div>` (обычный React-рефакторинг: вложенный
# `<span>` там уже есть), и группа окажется пустой. Раньше в этом случае
# extract_salary возвращал не None, а ПУСТОЙ Salary, из-за чего
# SalaryBlockStats считал блок НАЙДЕННЫМ: зарплата терялась у всех
# вакансий, а сторож, потребованный владельцем явно, молчал.

DRIFTED_BLOCKS = {
    "вложенный div перед суммой": (
        '<div data-qa="vacancy-salary"><div class="wrapper"></div><span>от 100 000 ₽</span></div>'
    ),
    "пустой блок": '<div data-qa="vacancy-salary"></div>',
    "блок из одной разметки": '<div data-qa="vacancy-salary"><span></span></div>',
}


@pytest.mark.parametrize("case", sorted(DRIFTED_BLOCKS))
def test_salary_block_without_text_is_drift_and_not_a_salary(case: str) -> None:
    """«Блок найден, но текста не дал» — это дрейф разметки, а не зарплата."""
    assert extract_salary(DRIFTED_BLOCKS[case]) is None


@pytest.mark.parametrize("case", sorted(DRIFTED_BLOCKS))
def test_drifted_block_is_not_counted_as_found(case: str, caplog: pytest.LogCaptureFixture) -> None:
    """Прогон, где блок отдал только разметку, обязан включать тот же сторож,
    что и прогон, где атрибут переименован: наблюдаемо это одно и то же —
    зарплата потеряна у всех."""
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"description": "<p>текст</p>"}</script>' + DRIFTED_BLOCKS[case]
    )
    stats = SalaryBlockStats()
    for _ in range(3):
        parse_vacancy_page(html, stats)
    assert (stats.pages, stats.without_salary) == (3, 3)
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "vacancy-salary" in caplog.text


def test_salary_not_stated_stays_a_legitimate_block() -> None:
    """Различение обязано сохраниться: «з/п не указана» — законный блок с
    непустым raw, а не дрейф, и сторож обязан молчать."""
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"description": "<p>текст</p>"}</script>'
        '<div data-qa="vacancy-salary"><span>з/п не указана</span></div>'
    )
    salary = extract_salary(html)
    assert salary is not None
    assert salary.raw == "з/п не указана"
    assert (salary.amount_from, salary.amount_to) == (None, None)
    stats = SalaryBlockStats()
    parse_vacancy_page(html, stats)
    assert (stats.pages, stats.without_salary) == (1, 0), "блок есть, дрейфа нет"


# --- дрейф разметки блока JSON-LD: валидная страница обязана разбираться ---
#
# Оба случая ниже — законная разметка, которую прежний разбор не узнавал, а
# «не узнал» здесь означает `FetchFailed` на ВАЛИДНОЙ странице: попытка
# обогащения сожжена, и после `max_attempts` вакансия уходит в
# `enrich_failed` терминально. Стоимость односторонняя, поэтому оба
# принимаются, хотя hh.ru сегодня пишет иначе.


def test_single_quoted_type_attribute_is_still_a_json_ld_block() -> None:
    """HTML разрешает одинарные кавычки вокруг значения атрибута."""
    html = (
        '<script type=\'application/ld+json\'>{"@type": "JobPosting", '
        '"description": "<p>Опыт Yocto</p>"}</script>'
    )
    details = parse_vacancy_page(html)
    assert details.description == "Опыт Yocto"


def test_type_given_as_a_list_is_still_a_job_posting() -> None:
    """`"@type": ["JobPosting", "Thing"]` — законная форма JSON-LD."""
    html = (
        '<script type="application/ld+json">'
        '{"@type": ["JobPosting", "Thing"], "description": "<p>Опыт Buildroot</p>"}'
        "</script>"
    )
    details = parse_vacancy_page(html)
    assert details.description == "Опыт Buildroot"


# --- Раунд «регион и формат работы»: формат работы со страницы вакансии ---


def test_work_formats_read_from_the_on_site_fixture() -> None:
    """Живая фикстура, а не синтетика: в этом проекте все Critical нашлись
    живыми данными."""
    assert extract_work_formats(load_fixture()) == frozenset({WorkFormat.ON_SITE})


def test_work_formats_read_from_the_remote_fixture() -> None:
    assert extract_work_formats(load_salary_fixture()) == frozenset({WorkFormat.REMOTE})


def test_several_formats_are_all_kept() -> None:
    """Вакансия может предлагать несколько форматов, и REMOTE не должен
    потеряться среди них (живой пример: Team Lead Go, три формата)."""
    html = (
        "&#34;workFormats&#34;:[{&#34;workFormatsElement&#34;:"
        "[&#34;ON_SITE&#34;,&#34;REMOTE&#34;,&#34;HYBRID&#34;]}]"
    )
    assert extract_work_formats(html) == frozenset(
        {WorkFormat.ON_SITE, WorkFormat.REMOTE, WorkFormat.HYBRID}
    )


def test_missing_block_gives_empty_set_not_an_error() -> None:
    """Отсутствие блока — не отказ страницы: формат необязателен, а дрейф
    ловится агрегатом, не одной страницей."""
    assert extract_work_formats("<html></html>") == frozenset()


def test_unknown_format_value_is_ignored_and_does_not_crash() -> None:
    """hh.ru может завести новое значение перечисления. Неизвестное
    отбрасывается, известные из того же списка сохраняются."""
    html = (
        "&#34;workFormats&#34;:[{&#34;workFormatsElement&#34;:"
        "[&#34;REMOTE&#34;,&#34;TELEPORT&#34;]}]"
    )
    assert extract_work_formats(html) == frozenset({WorkFormat.REMOTE})


def test_block_stats_shout_when_no_page_had_formats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сторож дрейфа: молчит на отдельной пустой странице, кричит на прогоне,
    где не нашлось ни одной. Тот же приём, что у SalaryBlockStats."""
    stats = WorkFormatBlockStats()
    for _ in range(5):
        stats.record(frozenset())
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "ни с одной" in caplog.text


def test_block_stats_stay_quiet_when_some_pages_had_formats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stats = WorkFormatBlockStats()
    stats.record(frozenset({WorkFormat.REMOTE}))
    stats.record(frozenset())
    with caplog.at_level(logging.WARNING):
        stats.log_summary()
    assert "ни с одной" not in caplog.text


def test_parse_vacancy_page_extracts_work_formats() -> None:
    """Формат — из того же разбора страницы, что и всё остальное: шаг
    обогащения получает его без отдельного похода в сеть."""
    assert parse_vacancy_page(load_fixture()).work_formats == frozenset({WorkFormat.ON_SITE})
    assert parse_vacancy_page(load_salary_fixture()).work_formats == frozenset({WorkFormat.REMOTE})


def test_work_format_stats_record_through_parse_vacancy_page() -> None:
    """Проводка до сторожа — по образцу salary_stats в той же функции."""
    stats = WorkFormatBlockStats()
    parse_vacancy_page(load_fixture(), work_format_stats=stats)
    parse_vacancy_page(load_salary_fixture(), work_format_stats=stats)
    assert (stats.pages, stats.without_formats) == (2, 0)
