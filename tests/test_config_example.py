"""Образцы конфигурации обязаны работать, а не только выглядеть работающими.

Два разных класса дефекта, и оба тихие:

1. Образец не проходит `load_config` — тогда первое, что видит новый
   пользователь после `cp config.example/*.yaml data/config/`, это падение
   на старте. Прежняя редакция `queries.yaml` содержала поля `text`,
   `area`, `experience`, `employment`, `schedule`, `period`, удалённые
   вместе с RSS-запросом, и `extra="forbid"` роняла её целиком.
2. Образец грузится, но сигналы в нём **молча не срабатывают**. Такой
   сигнал ничем не отличается от отсутствующего: он не роняет загрузку, не
   пишет в лог и просто не приносит вакансий. Ни ревью, ни исполнение
   этот класс не ловят — только прогон через фактический `SignalMatcher`.
"""

from pathlib import Path

import pytest

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.filtering.matching import SignalMatcher
from hh_search.sources.http import PoliteClient

CONFIG_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example"

# Слева — заголовок в живой по форме записи, справа — сигнал, который обязан
# сработать. Каждая пара взята из находки: прежняя редакция образца молчала
# на всех, кроме одной.
MUST_MATCH = [
    ("Ведущий разработчик C++", "ведущ"),
    ("Ведущего разработчика C++", "ведущ"),
    ("Ведущему инженеру-программисту", "ведущ"),
    ("Старшего разработчика Python", "старш"),
    ("Тимлид backend-разработки", "тимлид"),
    ("Техлид платформы", "техлид"),
    ("Сеньор разработчик", "сеньор"),
    ("Team-Lead разработки", "team-lead"),
    ("Tech-Lead платформы", "tech-lead"),
    ("Разработчик под ARM64", "arm64"),
    ("Firmware-инженер ARMv7", "armv7"),
    ("Node.js разработчик", "node.js"),
    ("NodeJS backend developer", "nodejs"),
    ("LLMs engineer", "llms"),
    ("Инженер IIoT-платформы", "iiot"),
    ("Инженер ручного тестирования", "ручн тестиров"),
    ("Специалист по ручному тестированию", "ручн тестиров"),
    ("Оператор ПК", "оператор пк"),
    ("Оператор call-центра", "оператор call"),
    ("Оператор колл-центра", "оператор колл"),
    ("Оператор станка ЧПУ", "оператор станка"),
]

# Ложное срабатывание стоп-слова стоит вакансии до ближайшей правки конфига:
# она уходит в `rejected` и вернётся оттуда только следующим прогоном и только
# если конфиг изменился (решение владельца, `requeue_prefiltered`).
MUST_NOT_BE_REJECTED = [
    "Ведущий разработчик C++ в крупный оператор связи",
    "Старший инженер-программист, телеком",
    "Тимлид backend (микросервисы, Kafka)",
]


@pytest.fixture(scope="module")
def example_config() -> Config:
    return load_config(CONFIG_EXAMPLE)


def _signal_groups(config: Config) -> dict[str, list[list[str]]]:
    """Каждое поле — список ГРУПП написаний (спека §6, §7)."""
    signals = config.profile.signals
    return {
        "title_roles": signals.title_roles,
        "title_tech": signals.title_tech,
        "stack": signals.stack,
        "responsibilities": signals.responsibilities,
        "domain": signals.domain,
        "negative": config.profile.negative,
    }


def _signals_by_field(config: Config) -> dict[str, list[str]]:
    """То же самое, но плоско: группа для повтора не граница (спека §7)."""
    return {
        field: [signal for group in groups for signal in group]
        for field, groups in _signal_groups(config).items()
    }


def _all_signals(config: Config) -> list[str]:
    return [signal for field in _signals_by_field(config).values() for signal in field]


def _negative_matcher(config: Config) -> SignalMatcher:
    """Ровно так же, как строит его `Prefilter`.

    `profile.negative` — список ГРУПП, а не написаний, и передать его в
    `SignalMatcher` как есть нельзя: `_compile` получил бы список вместо
    строки. В отсеве группы разворачиваются, потому что там сигнал не
    участвует ни в каком насыщении, а становится отдельной причиной отказа.
    """
    return SignalMatcher(_signals_by_field(config)["negative"])


def test_example_config_loads(example_config: Config) -> None:
    """`cp config.example/*.yaml data/config/` обязан давать рабочий конфиг."""
    assert example_config.queries.queries
    assert example_config.app.sinks == ["csv", "markdown"]


def test_example_user_agent_builds_a_client(example_config: Config) -> None:
    """Заглушка `contact_email` обязана давать РАБОЧИЙ User-Agent.

    Третий тихий класс дефекта, найденный запуском контейнера: адрес
    проходит валидатор (`^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$` кириллицу не
    запрещает), подставляется в `user_agent`, а httpx кодирует заголовки в
    ascii — и КАЖДЫЙ прогон падает `UnicodeEncodeError` из глубины httpx,
    ещё до первого байта в сеть. В `serve` этот отказ ловится общим
    `except Exception`: демон вечно крутит пустой цикл раз в четыре часа, а
    `init-db` (единственная команда, которой клиент не нужен) отрабатывает
    штатно и создаёт иллюзию рабочей установки.

    Сеть здесь не задействуется: конструктор только собирает `httpx.Client`.
    """
    with PoliteClient(example_config.app.http, example_config.app.user_agent):
        pass


def test_every_example_signal_compiles(example_config: Config) -> None:
    """Пустой или неразбираемый сигнал упал бы прямо здесь."""
    SignalMatcher(_all_signals(example_config))


@pytest.mark.parametrize("field", sorted(_signal_groups(load_config(CONFIG_EXAMPLE))))
def test_example_field_has_no_duplicate_signals(example_config: Config, field: str) -> None:
    """Повтор ВНУТРИ поля удваивает вклад и штраф. Повтор МЕЖДУ полями
    (`python` в title_tech и в stack) — наоборот, замысел: это разные
    слагаемые формулы (спека §6)."""
    signals = _signals_by_field(example_config)[field]
    assert len(signals) == len(set(signals))


@pytest.mark.parametrize(("title", "signal"), MUST_MATCH)
def test_example_signal_actually_fires(example_config: Config, title: str, signal: str) -> None:
    """Сигнал есть в образце И срабатывает на заголовке, ради которого внесён."""
    assert signal in _all_signals(example_config), f"{signal!r} пропал из образца"
    assert SignalMatcher([signal]).has_any(title)


@pytest.mark.parametrize("title", MUST_NOT_BE_REJECTED)
def test_example_stop_words_do_not_reject_the_target(example_config: Config, title: str) -> None:
    """«крупный оператор связи» — это целевой телеком, а не колл-центр."""
    assert _negative_matcher(example_config).find(title) == []
