from hh_search.config.models import ProfileConfig
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails, WorkFormat
from hh_search.filtering.matching import SignalGroupMatcher

# Поля склеиваются в одну строку, и разделитель обязан быть не-пробельным.
# С обычным «\n» многословный сигнал совпадал ЧЕРЕЗ СТЫК: `\s+` между
# словами паттерна считает перевод строки обычным пробелом, поэтому
# заголовок, кончающийся на «оператор», и описание, начинающееся с «ПК»,
# давали стоп-слову «оператор пк» минус пятнадцать очков ниоткуда.
_FIELD_SEPARATOR = "\n|\n"


def _normalize_area(value: str) -> str:
    """Схлопывает пробелы и регистр для точного сравнения региона.

    Не подстрока: «Нижний Новгород» подстрокой сидит и в «Нижний Новгород и
    область», и мало ли в чём ещё, а штраф, не сработавший там, где обязан
    был, — молчаливая потеря дороже штрафа, сработавшего лишним. Цена точного
    сравнения — административные варианты («городской округ Нижний
    Новгород») приходится перечислять в `home_areas` явно, а не полагаться
    на совпадение по вхождению.
    """
    return " ".join(value.split()).lower()


class KeywordScorer:
    """Оценка по спискам ключевых слов из profile.yaml (спека §6).

    total = 100 × (w.title·title + w.stack·stack + w.resp·resp + w.domain·domain) − penalty,
    снизу подрезано нулём.

    Единица счёта в насыщении и в штрафе — ГРУППА написаний, а не паттерн:
    `arm`, `arm64`, `armv7`, `armv8` перечисляются в конфиге вынужденно
    (§6.1), но означают одну архитектуру. Дубликат внутри списка сигналов
    накручивал бы насыщение так же, как это делали написания, — он
    отвергается валидатором конфига, там же, где `_UniqueKeyLoader`
    отвергает повторный ключ YAML.
    """

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile
        signals = profile.signals
        self._title_roles = SignalGroupMatcher(signals.title_roles)
        self._title_tech = SignalGroupMatcher(signals.title_tech)
        self._stack = SignalGroupMatcher(signals.stack)
        self._responsibilities = SignalGroupMatcher(signals.responsibilities)
        self._domain = SignalGroupMatcher(signals.domain)
        self._negative = SignalGroupMatcher(profile.negative)
        # Нормализованные домашние регионы считаются один раз при построении
        # скорера, а не на каждой вакансии: `location.home_areas` короток
        # (единицы городов), но score() зовётся на сотнях вакансий за прогон.
        self._home_areas = (
            frozenset(_normalize_area(home) for home in profile.location.home_areas)
            if profile.location is not None
            else None
        )

    def _region_penalty(self, area: str | None, work_formats: frozenset[WorkFormat]) -> float:
        """Штраф за вакансию вне домашнего региона без удалёнки (Task 3 плана).

        Порядок обязателен, ровно как в `LocationConfig`: домашний регион не
        штрафуется ни при каком формате — иначе штраф убивал бы офис в
        родном городе. Иначе REMOTE среди форматов снимает штраф — вакансия
        может предлагать сразу несколько форматов, и одного REMOTE
        достаточно. Иначе штраф. Неизвестные регион (`area` пуст, состоит из
        одних пробелов или `None` — `_extract_locality` отдаёт
        `addressLocality` как есть, и пустая строка в JSON-LD страницы такая
        же неизвестность, как отсутствующее поле) и формат (`work_formats`
        пусто — блока на странице не нашлось или вакансия ещё не обогащена)
        штрафа не несут по одной и той же причине: штрафовать по
        отсутствующим данным нельзя.
        """
        location = self._profile.location
        if location is None or self._home_areas is None:
            return 0.0
        if area is None or not area.strip():
            return 0.0
        if _normalize_area(area) in self._home_areas:
            return 0.0
        if not work_formats or WorkFormat.REMOTE in work_formats:
            return 0.0
        return location.penalty_not_remote_elsewhere

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        title = discovered.title
        description = details.description
        # Компания читается и из details: листинг её не отдаёт, а в базу она
        # попадает тем же save_enriched, что и оценка, то есть ПОСЛЕ вызова
        # скорера. Только discovered.company — это потерянный домен у каждой
        # вакансии на первом прогоне.
        company = discovered.company or details.company or ""
        # Регион — по той же схеме, что и компания: листинг его тоже не
        # отдаёт надёжно, а details.area появляется только после обогащения.
        area = discovered.area or details.area

        roles = self._title_roles.find(title)
        tech = self._title_tech.find(title)
        stack = self._stack.find(description)
        responsibilities = self._responsibilities.find(description)
        domain = self._domain.find(f"{description}{_FIELD_SEPARATOR}{company}")
        # Штраф считается по склейке заголовка и описания одним поиском:
        # один сигнал — один штраф, сколько бы полей его ни содержало.
        negative = self._negative.find(f"{title}{_FIELD_SEPARATOR}{description}")

        title_component = 1.0 if roles and tech else (0.5 if roles or tech else 0.0)
        # Насыщение обязательно: без min(...) оценка измеряла бы длину
        # описания, а не релевантность (спека §6). Делитель ≥ 1 гарантирован
        # валидатором конфига — иначе здесь было бы деление на ноль уже
        # ПОСЛЕ похода в сеть.
        stack_component = min(len(stack) / self._profile.saturation.stack, 1.0)
        responsibilities_component = min(
            len(responsibilities) / self._profile.saturation.responsibilities, 1.0
        )
        domain_component = 1.0 if domain else 0.0

        weights = self._profile.weights
        weighted = (
            weights.title * title_component
            + weights.stack * stack_component
            + weights.responsibilities * responsibilities_component
            + weights.domain * domain_component
        )
        # Штраф пропорционален ЧИСЛУ стоп-сигналов: одно случайное слово не
        # убивает хорошую вакансию, три убивают (спека §6). Три написания
        # одного стоп-слова — по-прежнему один сигнал и один штраф.
        penalty = len(negative) * self._profile.penalty_per_signal + self._region_penalty(
            area, details.work_formats
        )
        # Верхнего clamp'а нет сознательно: компоненты ≤ 1.0, веса
        # неотрицательны и суммируются в 1.0 (валидатор `Weights`), штраф
        # неотрицателен — значит total ≤ 100 по построению, и min(..., 100)
        # был бы кодом, который не может исполниться ни на одном входе, то
        # есть и проверить его было бы нечем. Нижний нужен: штраф
        # утаскивает сумму в минус на любом мусорном заголовке.
        total = max(100.0 * weighted - penalty, 0.0)

        return ScoreBreakdown(
            title=title_component,
            stack=stack_component,
            responsibilities=responsibilities_component,
            domain=domain_component,
            penalty=penalty,
            # Округляется только total — число, которое человек сравнивает с
            # порогом. Компоненты остаются как есть: по ним арифметика §6
            # должна сходиться обратно, а 0.33 вместо 1/3 её ломает.
            total=round(total, 1),
            # В каждом списке — по одному элементу на ЗАСЧИТАННЫЙ сигнал, а
            # внутри элемента через « / » перечислены конкретные написания,
            # из-за которых он засчитан. Так разбивка отвечает разом и на
            # «какие слова совпали», и на «сколько сигналов набрано»:
            # len(matched["stack"]) — это ровно числитель насыщения.
            matched={
                "title_roles": roles,
                "title_tech": tech,
                "stack": stack,
                "responsibilities": responsibilities,
                "domain": domain,
                "negative": negative,
            },
        )
