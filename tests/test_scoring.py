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
