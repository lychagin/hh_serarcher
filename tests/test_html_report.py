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
from hh_search.sinks.text import SNIPPET_LENGTH

NOW = datetime(2026, 7, 29, 10, 15, tzinfo=UTC)


def vacancy(
    vacancy_id: str = "1",
    title: str = "Backend-разработчик",
    company: str | None = "Р-Софт",
    total: float = 80.0,
    cluster: str = "backend",
    description: str = "Описание вакансии",
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
            description=description,
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


def test_top_entry_carries_the_beginning_of_the_description() -> None:
    """§6: «структура повторяет markdown-отчёт», а тот кладёт начало описания.

    Обещание §6 и файл разошлись: HTML — это ТОТ отчёт, который владелец
    читает с телефона вместо markdown, и без выжимки он был строго беднее
    того, что заменяет. Ради кликабельности ссылок выжимку не выбрасывали.
    """
    section = render_section([vacancy(total=90.0)], NOW, 60.0)
    assert "Описание вакансии" in section


def test_rest_entry_stays_a_one_liner_like_in_markdown() -> None:
    """Выжимка идёт только в «Топ» — ровно как `_full_entry` против
    `_short_entry` в markdown-отчёте. Порог меняет подробность показа."""
    section = render_section([vacancy(total=12.0)], NOW, 60.0)
    assert "Описание вакансии" not in section


def test_description_excerpt_is_cut_and_collapsed() -> None:
    """Описание с hh.ru — килобайты многострочного текста.

    Обрезка той же длины, что в markdown (общий `SNIPPET_LENGTH`), и
    переводы строк складываются в одну строку: `html_to_text` ставит их на
    месте блочных тегов, а в HTML они всё равно не видны — зато мешают
    grep'у по файлу.
    """
    section = render_section(
        [vacancy(total=90.0, description="раз.\n\nдва." + "я" * 500)], NOW, 60.0
    )
    assert "раз. два." in section
    assert "я" * (SNIPPET_LENGTH + 1) not in section


def test_description_excerpt_is_escaped() -> None:
    """Описание пишет работодатель — значит в нём есть что угодно."""
    section = render_section([vacancy(total=90.0, description="C++ <script>x</script>")], NOW, 60.0)
    assert "<script>" not in section
    assert "C++ &lt;script&gt;" in section


def test_header_is_self_contained() -> None:
    """Файл открывают с диска телефона, часто без сети: внешних ресурсов нет."""
    header = document_header(NOW)
    assert 'charset="utf-8"' in header
    assert "viewport" in header
    assert "http://" not in header
    assert "https://" not in header
