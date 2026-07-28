import gzip
from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.models import ProfileConfig, Saturation, Signals, Weights
from hh_search.domain.models import DiscoveredVacancy
from hh_search.filtering.matching import SignalMatcher
from hh_search.filtering.prefilter import Prefilter
from hh_search.sources.listing import parse_listing

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_LISTING = "listing_programmist.html.gz"

# Стоп-слова образца profile.yaml из спеки §7 — ровно те, что поедут в прод.
SPEC_NEGATIVE = [
    "junior",
    "стажёр",
    "intern",
    "1c",
    "продаж",
    "рекрутер",
    "ручн тестиров",
    "оператор пк",
    "оператор call",
    "оператор колл",
    "оператор станка",
    "курьер",
]


def make_profile(negative: list[str]) -> ProfileConfig:
    """Профиль, в котором заполнено только то, что читает префильтр.

    Позитивные сигналы намеренно пусты: на шаге 3 они не участвуют вовсе,
    и пустые списки это фиксируют лучше любого комментария.
    """
    return ProfileConfig(
        weights=Weights(title=0.4, stack=0.3, responsibilities=0.2, domain=0.1),
        saturation=Saturation(stack=5, responsibilities=3),
        penalty_per_signal=15,
        signals=Signals(
            title_roles=[],
            title_tech=[],
            stack=[],
            responsibilities=[],
            domain=[],
        ),
        negative=negative,
    )


def make_vacancy(title: str, company: str | None = None) -> DiscoveredVacancy:
    """Ровно то, что даёт листинг: id, url, title (спека §3.2)."""
    return DiscoveredVacancy(
        id="1",
        url="https://hh.ru/vacancy/1",
        title=title,
        company=company,
        found_by_query="programmist",
    )


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


def test_clean_title_passes() -> None:
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    assert prefilter.reason_to_reject(make_vacancy("Backend Team Lead")) is None


def test_reason_names_the_stop_word_that_decided() -> None:
    """Причина уходит в `reject_reason` и остаётся единственным следом
    решения: без названного слова отладку списка сигналов не провести."""
    reason = Prefilter(make_profile(SPEC_NEGATIVE)).reason_to_reject(
        make_vacancy("Junior Python Developer")
    )
    assert reason == "стоп-слово в заголовке: junior"


def test_reason_lists_every_matched_stop_word_in_config_order() -> None:
    """Три совпадения — три слова в причине. Первое из них ничем не лучше
    остальных, а «одно слово из трёх» превращает отладку в угадывание."""
    reason = Prefilter(make_profile(SPEC_NEGATIVE)).reason_to_reject(
        make_vacancy("Программист 1С (стажер/junior)")
    )
    assert reason == "стоп-слово в заголовке: junior, стажёр, 1c"


def test_empty_negative_list_rejects_nothing() -> None:
    """Профиль без стоп-слов — законная конфигурация: конвейер тогда просто
    не отсеивает ничего локально, а не отсеивает всё."""
    prefilter = Prefilter(make_profile([]))
    assert prefilter.reason_to_reject(make_vacancy("Курьер на личном автомобиле")) is None


def test_only_the_title_is_examined() -> None:
    """На шаге 3 известен только заголовок (спека §3.2): компания и регион
    приходят со страницы вакансии, то есть уже после оплаты запросом.
    Стоп-слово в поле, которого у листинга нет, отказом быть не может."""
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    vacancy = make_vacancy("Инженер-программист", company="Продажи и курьеры")
    assert prefilter.reason_to_reject(vacancy) is None


def test_empty_stop_word_cannot_reach_the_prefilter() -> None:
    """Страховка на самое дорогое: пустой сигнал компилируется в регулярку
    из одних границ слова и отбраковывает почти любой заголовок — молча и
    необратимо. Отвергается дважды, и оба раза проверяются здесь."""
    with pytest.raises(ValidationError):
        make_profile([""])
    with pytest.raises(ValueError, match="пустой сигнал"):
        SignalMatcher([" "])


def test_no_good_title_is_lost_on_the_live_listing() -> None:
    """Живая страница `/vacancies/programmist`, 20 настоящих заголовков.

    Проверяется список выживших ЦЕЛИКОМ, а не только число отказов: ложный
    отказ выбрасывает хорошую вакансию навсегда, и это самая дорогая
    ошибка конвейера. Список зафиксирован по факту прогона; любое
    расширение стоп-слов, задевающее эти девять заголовков, обязано
    покраснеть здесь.
    """
    prefilter = Prefilter(make_profile(SPEC_NEGATIVE))
    vacancies = parse_listing(load(LIVE_LISTING), "programmist")
    assert len(vacancies) == 20

    survived = [v.title for v in vacancies if prefilter.reason_to_reject(v) is None]
    assert survived == [
        "Программист: WinForms (MVP), C#, .NET",
        "Программист-разработчик С#",
        "Java разработчик (ученик)",
        "Программист .Net",
        "Разработчик систем извлечения данных",
        "Инженер-программист",
        "Преподаватель для младшей школы (программирование и ИТ)",
        "Программист на ПО Fansy (SPECTRE, DEPO)",
        "Программист SQL/Delphi",
    ]
