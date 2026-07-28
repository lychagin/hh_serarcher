"""Тесты разбора строки зарплаты.

Функция переехала из `sources/rss.py` в `sources/salary.py` (RSS выключен
запретом robots.txt, а зарплата приходит теперь из разметки страницы
вакансии), поэтому тесты переехали вместе с ней — дословно, без единого
изменения проверок.
"""

import pytest

from hh_search.sources.salary import parse_salary


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
    assert parse_salary("от\xa0200\xa0000\xa0руб.").amount_from == 200000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("от 250 000 ₽", (250000, None, "₽")),
        ("от 900 000 до 1 300 000 ₸", (900000, 1300000, "₸")),
        ("от\xa0100\xa0000\xa0₽", (100000, None, "₽")),
        ("до 2000 EUR", (None, 2000, "EUR")),
        ("до 2000 USD", (None, 2000, "USD")),
    ],
)
def test_parse_salary_extracts_currency_symbols(
    raw: str, expected: tuple[int | None, int | None, str | None]
) -> None:
    salary = parse_salary(raw)
    assert (salary.amount_from, salary.amount_to, salary.currency) == expected


@pytest.mark.parametrize(
    ("raw", "expected_currency"),
    [
        ("от 200 000 до 300 000 руб. на руки", "руб."),
        ("до 2000 EUR на руки", "EUR"),
    ],
)
def test_parse_salary_ignores_trailing_words_after_currency(
    raw: str, expected_currency: str
) -> None:
    assert parse_salary(raw).currency == expected_currency


def test_parse_salary_range_without_currency_yields_none() -> None:
    salary = parse_salary("от 200 000 до 300 000")
    assert (salary.amount_from, salary.amount_to, salary.currency) == (200000, 300000, None)


def test_parse_salary_ignores_digit_appearing_after_currency() -> None:
    salary = parse_salary("от 100 000 ₽, обсуждается на собеседовании 2")
    assert salary.currency == "₽"


def test_parse_salary_handles_currency_glued_to_amount() -> None:
    assert parse_salary("от 100 000₽").currency == "₽"


def test_parse_salary_ignores_periodicity_suffix() -> None:
    assert parse_salary("от 100 000 ₽ в месяц").currency == "₽"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("от 200 000 до 300 000", (200000, 300000, None)),
        ("от 900 000 до 1 300 000 ₸", (900000, 1300000, "₸")),
        ("от 250 000 ₽", (250000, None, "₽")),
        ("от 200 000 до 300 000 руб. на руки", (200000, 300000, "руб.")),
        ("до 2000 EUR на руки", (None, 2000, "EUR")),
        ("от 100 000 ₽ в месяц", (100000, None, "₽")),
        ("от 100 000", (100000, None, None)),
        ("не указан", (None, None, None)),
        ("", (None, None, None)),
        ("от 100 000 ₽, обсуждается на собеседовании 2", (100000, None, "₽")),
        ("от 100 000₽", (100000, None, "₽")),
        # «до вычета налогов» — не сумма: слот верхней границы остаётся пустым.
        ("от 100 000 ₽ до вычета налогов", (100000, None, "₽")),
        # Хвостовая проза после валюты в разбор не входит вообще.
        ("от 100 000 ₽, опыт от 3 лет", (100000, None, "₽")),
        ("от 100 000 ₽ на руки, отпуск от 28 дней", (100000, None, "₽")),
        ("от 100 000 до 200 000 ₽ до 31 декабря", (100000, 200000, "₽")),
        ("от 100 000 до 200 000 ₽, опыт от 3 лет", (100000, 200000, "₽")),
        # Второй диапазон намеренно игнорируется: якорный разбор читает ОДНО выражение
        # от начала строки, поэтому суммы берутся из первого диапазона, а в слот валюты
        # попадает стоящее там «или». Поле дохода hh.ru двух диапазонов не содержит —
        # размен принят осознанно в пользу устойчивости на реальных данных.
        ("от 100 000 до 200 000 или от 300 000 до 400 000 ₽", (100000, 200000, "или")),
        # Дробная часть входит в число (и отбрасывается), а не становится валютой.
        ("от 100 000.50 ₽", (100000, None, "₽")),
        # Отрицательная сумма не является суммой, но валюту это не ломает.
        ("от -100 000 ₽", (None, None, "₽")),
    ],
)
def test_parse_salary_control_table(
    raw: str, expected: tuple[int | None, int | None, str | None]
) -> None:
    salary = parse_salary(raw)
    assert (salary.amount_from, salary.amount_to, salary.currency) == expected


def test_parse_salary_survives_absurdly_long_number() -> None:
    assert parse_salary("от " + "1 " * 10_000 + "₽").amount_from is None


@pytest.mark.parametrize(
    "tail",
    [
        "",
        " на руки",
        " в месяц",
        " до вычета налогов",
        ", опыт от 3 лет",
        ", отпуск от 28 дней",
        " до 31 декабря",
        " или от 300 000 до 400 000 $",
        " (оклад не указан явно)",
        " ₸ EUR USD",
        " 12345",
    ],
)
def test_parse_salary_tail_after_currency_never_participates(tail: str) -> None:
    salary = parse_salary("от 100 000 ₽" + tail)
    assert (salary.amount_from, salary.amount_to, salary.currency) == (100000, None, "₽")


def test_parse_salary_ignores_keyword_inside_word() -> None:
    # «работ» содержит «от», но границей диапазона это слово не делает.
    salary = parse_salary("оплата за 5 работ 100 000 ₽")
    assert (salary.amount_from, salary.amount_to, salary.currency) == (None, None, None)
