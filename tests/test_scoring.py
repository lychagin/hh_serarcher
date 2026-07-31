import gzip
from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.models import LocationConfig, ProfileConfig, Saturation, Signals, Weights
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails, WorkFormat
from hh_search.scoring.keyword import KeywordScorer
from hh_search.sources.vacancy_page import parse_vacancy_page

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_VACANCY = "vacancy.html.gz"


# Элемент списка сигналов — либо одно написание, либо группа написаний
# одной сущности (спека §6, §7).
SignalList = list[str | list[str]]

DEFAULT_STACK: SignalList = ["yocto", "buildroot", "c++", "kubernetes", "kafka", "docker"]
DEFAULT_DOMAIN: SignalList = ["телеком"]
DEFAULT_NEGATIVE: SignalList = ["junior", "1c", "продаж"]


def make_profile(
    stack: SignalList | None = None,
    domain: SignalList | None = None,
    negative: SignalList | None = None,
    location: LocationConfig | None = None,
) -> ProfileConfig:
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
            stack=DEFAULT_STACK if stack is None else stack,
            responsibilities=["архитектур", "менторинг", "код-ревью", "проектирован"],
            domain=DEFAULT_DOMAIN if domain is None else domain,
        ),
        negative=DEFAULT_NEGATIVE if negative is None else negative,
        location=location,
    )


def score_for(
    title: str,
    description: str,
    company: str | None = None,
    page_company: str | None = None,
    stack: SignalList | None = None,
    domain: SignalList | None = None,
    negative: SignalList | None = None,
    location: LocationConfig | None = None,
    area: str | None = "Нижний Новгород",
    work_formats: frozenset[WorkFormat] = frozenset(),
) -> ScoreBreakdown:
    discovered = DiscoveredVacancy(
        id="1",
        url="https://hh.ru/vacancy/1",
        title=title,
        company=company,
        area=area,
        found_by_query="programmist",
    )
    # area дублируется в details: `discovered.area or details.area` в KeywordScorer.score
    # заменяет ЛЮБУЮ ложную область (не только None, но и "") на details.area, а стенд
    # передаёт единственное значение area — записать его только в discovered потеряло бы
    # пустую строку по дороге и сделало бы тест на неё вакуумным.
    details = VacancyDetails(
        description=description, company=page_company, work_formats=work_formats, area=area
    )
    profile = make_profile(stack=stack, domain=domain, negative=negative, location=location)
    return KeywordScorer(profile).score(discovered, details)


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


def test_stop_word_only_in_the_title_is_penalised() -> None:
    """Штраф считается по склейке заголовка и описания. Единственный
    прежний тест с заголовочным стоп-словом дублировал его в описании,
    поэтому «ищем только в описании» не краснело нигде."""
    result = score_for("Junior Engineer", "Разработка сервисов на C++")
    assert result.matched["negative"] == ["junior"]
    assert result.penalty == 15.0


def test_stop_word_only_in_the_description_is_penalised() -> None:
    result = score_for("Инженер", "Ищем junior-разработчика в команду")
    assert result.matched["negative"] == ["junior"]
    assert result.penalty == 15.0


def test_stop_word_in_both_fields_costs_one_penalty() -> None:
    """Один сигнал — один штраф. Если считать поля по отдельности, слово,
    встретившееся и в заголовке, и в описании (самый частый случай),
    стоило бы вакансии тридцать очков вместо пятнадцати."""
    result = score_for("Junior Engineer", "Ищем junior-разработчика в команду")
    assert result.matched["negative"] == ["junior"]
    assert result.penalty == 15.0


def test_stack_and_responsibilities_are_read_from_the_description_only() -> None:
    """§6: stack и responsibilities считаются ПО ПОЛНОМУ ОПИСАНИЮ, заголовок
    отработан отдельным компонентом `title`. Если искать стек ещё и в
    заголовке, «Yocto/C++/Docker» в названии добавит 30 очков сверх тех 40,
    которые за то же самое уже начислил `title`."""
    result = score_for("Yocto Buildroot C++ Kubernetes Kafka Docker, архитектура", "")
    assert result.matched["stack"] == []
    assert result.matched["responsibilities"] == []
    assert (result.stack, result.responsibilities) == (0.0, 0.0)


def test_components_of_the_breakdown_are_not_rounded() -> None:
    """Округляется только `total`. Компоненты остаются как есть: по ним
    арифметика §6 должна сходиться обратно, а 0.33 вместо 1/3 её ломает —
    и ломает молча, потому что нецелого компонента не проверял ни один
    прежний тест."""
    result = score_for("Инженер", "Участие в архитектуре подсистем")
    assert result.responsibilities == 1 / 3
    assert result.responsibilities != round(1 / 3, 2)
    # С округлением компонентов до двух знаков было бы 6.6.
    assert result.total == 6.7


def test_total_is_rounded_and_not_rounded_up() -> None:
    """13.333… → 13.3, а не 13.4. Направление округления фиксируется
    отдельно: «округлить» и «округлить вверх» совпадают на большинстве
    входов и расходятся ровно там, где балл сравнивают с порогом."""
    result = score_for("Инженер", "Архитектура сервисов и менторинг команды")
    assert result.responsibilities == 2 / 3
    assert result.total == 13.3


def test_breakdown_names_the_words_behind_every_component() -> None:
    """Шесть списков разбивки — это ответ на вопрос «почему 87?» (§6).
    Три из них (`domain`, `title_tech`, `responsibilities`) не проверялись
    на непустоту ни разу: подмена любого из них пустым списком проходила
    весь набор тестов зелёной."""
    result = score_for(
        "Senior Embedded Engineer",
        "Архитектура сервисов, опыт Yocto",
        company="Телеком Решения",
    )
    assert result.matched["title_roles"] == ["senior"]
    assert result.matched["title_tech"] == ["embedded"]
    assert result.matched["stack"] == ["yocto"]
    assert result.matched["responsibilities"] == ["архитектур"]
    assert result.matched["domain"] == ["телеком"]


def test_company_from_the_listing_wins_over_the_one_from_the_page() -> None:
    """Порядок именно такой, потому что в отчёт печатается
    `discovered.company`: балл обязан объясняться той же строкой, которую
    читатель отчёта видит глазами. `details.company` — не приоритет, а
    запасной вариант на первый прогон, когда листинг компании не дал."""
    result = score_for("Инженер", "", company="Рога и Копыта", page_company="Телеком Решения")
    assert result.matched["domain"] == []
    assert result.domain == 0.0


def test_matched_lists_follow_config_order() -> None:
    """Порядок в разбивке — порядок КОНФИГА, а не порядок вхождения в текст.
    В описании сначала Kafka, в конфиге сначала yocto."""
    result = score_for("Senior Embedded Engineer", "Опыт Kafka и Yocto")
    assert result.matched["stack"] == ["yocto", "kafka"]
    assert result.matched["title_roles"] == ["senior"]


# --- группы синонимов -----------------------------------------------------

ARM_STACK: SignalList = [
    ["arm", "arm64", "armv7", "armv8"],
    "yocto",
    "docker",
    "kafka",
    "python",
]


def test_spellings_of_one_technology_count_as_one_signal() -> None:
    """§6.1 обязывает писать `arm`, `arm64`, `armv7`, `armv8` отдельными
    паттернами: правая граница запрещает букву и цифру вплотную. Если
    считать их четырьмя сигналами, описание, упоминающее ОДНО семейство
    процессоров, набирает столько же, сколько описание с четырьмя разными
    технологиями. Группа — один сигнал насыщения."""
    result = score_for("Инженер", "Опыт ARM, ARM64, ARMv7 и ARMv8", stack=ARM_STACK)
    assert result.matched["stack"] == ["arm / arm64 / armv7 / armv8"]
    assert result.stack == 0.2
    # Прежде было 4/5 = 0.8 и 24 очка из 30 за одну архитектуру.
    assert result.total == 6.0


def test_five_different_technologies_still_saturate() -> None:
    """Обратная сторона той же проверки: группировка не должна занижать
    вакансию, в которой технологии действительно разные."""
    result = score_for("Инженер", "ARM, Yocto, Docker, Kafka, Python в проекте", stack=ARM_STACK)
    assert result.matched["stack"] == ["arm", "yocto", "docker", "kafka", "python"]
    assert result.stack == 1.0


def test_matched_shows_the_spellings_and_the_number_of_counted_signals() -> None:
    """Разбивка обязана отвечать и «какие слова совпали», и «сколько
    сигналов засчитано». Совпавшие написания одной группы склеены в один
    элемент, поэтому длина списка — это ровно числитель насыщения."""
    result = score_for("Инженер", "Сборка под ARM64 и ARMv7, Docker", stack=ARM_STACK)
    assert result.matched["stack"] == ["arm64 / armv7", "docker"]
    assert len(result.matched["stack"]) == 2
    assert result.stack == 0.4


def test_spellings_of_one_stop_word_cost_one_penalty() -> None:
    """Три написания одного стоп-слова — не три причины отказать."""
    result = score_for(
        "Оператор ПК",
        "Оператор call-центра, он же оператор колл-центра",
        negative=[["оператор пк", "оператор call", "оператор колл"]],
    )
    assert result.matched["negative"] == ["оператор пк / оператор call / оператор колл"]
    assert result.penalty == 15.0


# --- склейка полей --------------------------------------------------------


def test_multiword_signal_does_not_match_across_title_and_description() -> None:
    """Поля склеиваются в одну строку, и `\\s+` между словами паттерна
    считает перевод строки обычным пробелом. Заголовок, кончающийся на
    «оператор», и описание, начинающееся с «ПК», давали стоп-слову
    «оператор пк» совпасть через стык — минус пятнадцать очков ниоткуда."""
    result = score_for(
        "Ведущий оператор",
        "ПК и серверы в парке компании",
        negative=[["оператор пк"]],
    )
    assert result.matched["negative"] == []
    assert result.penalty == 0.0


def test_multiword_signal_does_not_match_across_description_and_company() -> None:
    result = score_for(
        "Инженер",
        "Телефонная связь",
        company="Москва",
        domain=[["связь москва"]],
    )
    assert result.matched["domain"] == []
    assert result.domain == 0.0


# --- живая страница -------------------------------------------------------


def spec_profile() -> ProfileConfig:
    """Образец profile.yaml из спеки §7 — тот, что поедет в прод.

    Написания одной сущности собраны в группы: `arm`/`arm64`/`armv7`/`armv8`
    перечислены отдельными паттернами вынужденно (§6.1), но означают одну
    архитектуру и считаются одним сигналом.
    """
    return ProfileConfig(
        weights=Weights(title=0.40, stack=0.30, responsibilities=0.20, domain=0.10),
        saturation=Saturation(stack=5, responsibilities=3),
        penalty_per_signal=15,
        signals=Signals(
            title_roles=[
                ["team lead", "tech lead", "teamlead"],
                "senior",
                "ведущ",
                "старш",
                "руководител",
            ],
            title_tech=[
                "backend",
                "embedded",
                "linux",
                "c++",
                "python",
                ["node", "node.js", "nodejs"],
                "firmware",
            ],
            stack=[
                "yocto",
                "buildroot",
                "openwrt",
                "bsp",
                "kernel",
                ["arm", "arm64", "armv7", "armv8"],
                "c++",
                "python",
                "node.js",
                "typescript",
                "docker",
                "kubernetes",
                "kafka",
                "postgresql",
                "clickhouse",
                "llm",
                "rag",
                "mcp",
            ],
            responsibilities=[
                "архитектур",
                "менторинг",
                ["код-ревью", "code review"],
                "проектирован",
                "техдолг",
            ],
            domain=["телеком", "встраиваем", "embedded", "iot", "микросервис"],
        ),
        negative=[
            ["junior", "стажёр", "intern"],
            "1c",
            "продаж",
            "рекрутер",
            "ручн тестиров",
            ["оператор пк", "оператор call", "оператор колл", "оператор станка"],
            "курьер",
        ],
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
    совпало шесть сигналов стека при насыщении 5 — семь написаний, из
    которых `arm` и `arm64` считаются одной архитектурой.
    """
    details = parse_vacancy_page(load(LIVE_VACANCY))
    discovered = DiscoveredVacancy(
        id="135586311",
        url="https://hh.ru/vacancy/135586311",
        title="Старший инженер-разработчик Embedded Linux (BSP, ARM64, i.MX 8M Plus)",
        found_by_query="programmist",
    )
    result = KeywordScorer(spec_profile()).score(discovered, details)
    assert result.matched["stack"] == [
        "yocto",
        "buildroot",
        "bsp",
        "kernel",
        "arm / arm64",
        "c++",
    ]
    assert result.matched["responsibilities"] == []
    assert (result.title, result.stack, result.responsibilities, result.domain) == (
        1.0,
        1.0,
        0.0,
        1.0,
    )
    assert result.penalty == 0.0
    assert result.total == 80.0


# --- штраф за неудалённую работу вне домашнего региона (Task 3) -----------

LOCATION = LocationConfig(
    home_areas=["Нижний Новгород", "Дзержинск"], penalty_not_remote_elsewhere=40
)

# Заголовок и описание подобраны так, чтобы БЕЗ штрафа балл был заметно выше
# нуля: иначе штраф не отличить от пола оценки, и тест проходил бы вакуумно.
STRONG_TITLE = "Team Lead backend"
STRONG_BODY = "архитектур, менторинг, код-ревью, c++, kubernetes, kafka, docker, телеком"


def test_home_area_is_not_penalised_whatever_the_format() -> None:
    """Домашний регион побеждает формат: офис в родном городе подходит."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    office_at_home = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Нижний Новгород",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert office_at_home.total == plain.total


def test_second_home_area_also_counts() -> None:
    """`home_areas` — список: второй город в нём не хуже первого."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Дзержинск",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_area_is_normalised_before_comparison() -> None:
    """Сравнение — после нормализации пробелов и регистра (бриф Step 3), не
    посимвольно: лишние пробелы и другой регистр не должны включать штраф,
    иначе `_normalize_area` можно удалить, и набор промолчит."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="  нижний   НОВГОРОД ",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_remote_elsewhere_is_not_penalised() -> None:
    """Удалёнка вне дома — законный случай: штрафа нет."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Москва",
        work_formats=frozenset({WorkFormat.REMOTE}),
    )
    assert result.total == plain.total


def test_office_elsewhere_is_penalised() -> None:
    """Вне дома и не удалённо — ровно тот случай, ради которого штраф введён."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Казань",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total < plain.total
    assert plain.total - result.total == 40


def test_hybrid_elsewhere_is_penalised() -> None:
    """Живой случай из «Топа»: гибрид вне дома штрафуется тем же правилом,
    что и чистый офис — REMOTE среди форматов не заявлен."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Санкт-Петербург",
        work_formats=frozenset({WorkFormat.HYBRID}),
    )
    assert result.total < plain.total


def test_remote_among_several_formats_is_enough() -> None:
    """Вакансия может предлагать сразу несколько форматов: одного REMOTE
    среди них достаточно, отменять остальные не нужно."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Москва",
        work_formats=frozenset({WorkFormat.ON_SITE, WorkFormat.REMOTE, WorkFormat.HYBRID}),
    )
    assert result.total == plain.total


def test_unknown_format_is_not_penalised() -> None:
    """Пустое множество форматов — блока на странице не нашлось или вакансия
    ещё не обогащена (см. `VacancyDetails.work_formats`), а не «не удалённо».
    Штрафовать за незнание нельзя."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Москва",
        work_formats=frozenset(),
    )
    assert result.total == plain.total


def test_unknown_area_is_not_penalised() -> None:
    """Неизвестный регион — та же причина, что и неизвестный формат: штрафовать
    по отсутствующим данным нельзя."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area=None,
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_empty_area_is_not_penalised() -> None:
    """Пустая строка — то же незнание региона, что и `None`: `_extract_locality`
    (`hh_search/sources/vacancy_page.py`) отдаёт `addressLocality` из JSON-LD
    как есть, без проверки на пустоту, и `""` там законный результат."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_whitespace_only_area_is_not_penalised() -> None:
    """Строка из одних пробелов — та же пустота, просто не пойманная `not area`."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="   ",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_penalty_lands_in_score_detail() -> None:
    """Штраф обязан быть виден не только в `total`, но и в разбивке `penalty`."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="Казань",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.penalty - plain.penalty == 40


def test_score_never_goes_below_zero() -> None:
    """Штраф за регион подрезается тем же нижним пределом, что и штраф за
    стоп-слова: сумма штрафов не уводит `total` в минус.

    Слабый заголовок из таблицы брифа делает `weighted == 0`, и `total == 0.0`
    держится ДАЖЕ при штрафе, тождественно равном нулю, — такой тест ловил бы
    только вычитание штрафа ПОСЛЕ clamp'а, а не сам факт клампа. Решение
    контроллера (fix-раунд 1): внутреннее противоречие брифа между строкой
    таблицы («слабый заголовок») и шапкой (`STRONG_TITLE`/`STRONG_BODY`,
    комментарий про «пол оценки») разрешено в пользу шапки — сильная вакансия
    набирает 94.0 без штрафа, штраф 100 уводит сумму в минус, и именно это
    здесь проверяется."""
    strong_location = LocationConfig(
        home_areas=["Нижний Новгород"], penalty_not_remote_elsewhere=100
    )
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=strong_location,
        area="Казань",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == 0.0


def test_profile_without_location_section_scores_as_before() -> None:
    """`location` не задан — штрафа нет вовсе: старые профили без раздела
    `location` продолжают работать как раньше."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=None,
        area="Казань",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total == plain.total


def test_penalty_above_hundred_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LocationConfig(home_areas=["X"], penalty_not_remote_elsewhere=400)


def test_empty_home_areas_is_rejected() -> None:
    """Раздел есть, а домашнего региона нет — опечатка, при которой штраф
    ловит абсолютно всё, включая вакансии в родном городе автора конфига."""
    with pytest.raises(ValidationError):
        LocationConfig(home_areas=[], penalty_not_remote_elsewhere=40)


def test_administrative_prefix_variant_is_not_recognised_as_home() -> None:
    """Сравнение региона — точное, не по подстроке (см. `_normalize_area`).

    «городской округ Нижний Новгород» — реальное значение `area` из живой
    базы (§Step 4 брифа), не равное «Нижний Новгород» посимвольно. Оно
    ЗАРАБОТАЕТ штраф, хотя фактически это тот же город: точное сравнение
    ловит опечатки в `home_areas` ценой того, что такие административные
    варианты приходится перечислять в конфиге явно — подстрочное сравнение
    было бы дешевле, но цена его ошибки выше (см. докстринг `_normalize_area`
    и «Нижний Новгород и область»)."""
    plain = score_for(STRONG_TITLE, STRONG_BODY)
    result = score_for(
        STRONG_TITLE,
        STRONG_BODY,
        location=LOCATION,
        area="городской округ Нижний Новгород",
        work_formats=frozenset({WorkFormat.ON_SITE}),
    )
    assert result.total < plain.total
