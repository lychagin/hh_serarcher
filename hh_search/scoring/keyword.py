from hh_search.config.models import ProfileConfig
from hh_search.domain.models import DiscoveredVacancy, ScoreBreakdown, VacancyDetails
from hh_search.filtering.matching import SignalMatcher


class KeywordScorer:
    """Оценка по спискам ключевых слов из profile.yaml (спека §6).

    total = 100 × (w.title·title + w.stack·stack + w.resp·resp + w.domain·domain) − penalty,
    снизу подрезано нулём.

    Известное ограничение: дубликат в списке сигналов накручивает
    насыщение. `stack: [yocto, yocto, yocto, yocto, yocto]` при
    `saturation.stack = 5` даёт 1.0 на описании с одним словом — `find`
    возвращает по одному вхождению на ПАТТЕРН, а не на уникальное слово.
    Ловить это в коде нечем: дубликат в YAML — законный ключ с законным
    значением. Проверяется глазами при правке профиля.
    """

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile
        signals = profile.signals
        self._title_roles = SignalMatcher(signals.title_roles)
        self._title_tech = SignalMatcher(signals.title_tech)
        self._stack = SignalMatcher(signals.stack)
        self._responsibilities = SignalMatcher(signals.responsibilities)
        self._domain = SignalMatcher(signals.domain)
        self._negative = SignalMatcher(profile.negative)

    def score(self, discovered: DiscoveredVacancy, details: VacancyDetails) -> ScoreBreakdown:
        title = discovered.title
        description = details.description
        # Компания читается и из details: листинг её не отдаёт, а в базу она
        # попадает тем же save_enriched, что и оценка, то есть ПОСЛЕ вызова
        # скорера. Только discovered.company — это потерянный домен у каждой
        # вакансии на первом прогоне.
        company = discovered.company or details.company or ""

        roles = self._title_roles.find(title)
        tech = self._title_tech.find(title)
        stack = self._stack.find(description)
        responsibilities = self._responsibilities.find(description)
        domain = self._domain.find(f"{description}\n{company}")
        negative = self._negative.find(f"{title}\n{description}")

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
        # Штраф пропорционален ЧИСЛУ стоп-слов: одно случайное слово не
        # убивает хорошую вакансию, три убивают (спека §6).
        penalty = len(negative) * self._profile.penalty_per_signal
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
            # должна сходиться обратно, а 0.67 вместо 1/3 её ломает.
            total=round(total, 1),
            matched={
                "title_roles": roles,
                "title_tech": tech,
                "stack": stack,
                "responsibilities": responsibilities,
                "domain": domain,
                "negative": negative,
            },
        )
