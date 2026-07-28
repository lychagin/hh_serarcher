import re
from pathlib import Path
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

# Нормализация берётся у матчера, а не пишется здесь заново: уникальность
# сигналов обязана проверяться в той же форме, в которую они компилируются,
# иначе `yocto` и ` YOCTO ` окажутся «разными» сигналами, будучи одной и
# той же регуляркой. Зависимость односторонняя — matching не знает о конфиге.
from hh_search.filtering.matching import normalize


class Base(BaseModel):
    # extra="forbid" ловит опечатку в ИМЕНИ ключа, но не в значении, а опечатка
    # в значении так же тиха и так же дорога: delay: 0 выключает вежливость к
    # hh.ru, saturation: 0 роняет скоринг делением на ноль уже после похода в
    # сеть. allow_inf_nan=False добивает то, что не ловится границами: `1e400`
    # в YAML — это inf, а NaN проходит сквозь любое сравнение как «истина не
    # нарушена». Всё вместе выполняет требование спеки §7: опечатка роняет
    # процесс на старте, до первого сетевого запроса.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


NonEmptyStr = Annotated[str, Field(min_length=1)]


def _reject_blank_and_collapse_spacing(value: str) -> str:
    if not value.strip():
        raise ValueError("пустой или пробельный сигнал недопустим: он совпадёт с любым текстом")
    # Пробелы по краям и лишние внутри на матч не влияют (паттерн всё равно
    # режется по словам), но уезжают как есть в `reject_reason` и в разбивку
    # оценки, а ещё делают ` yocto ` и `yocto` внешне разными сигналами.
    return " ".join(value.split())


# Пустой сигнал компилируется в регулярку из одних границ слова и матчит почти
# любой заголовок. В отсеве это необратимо (status='rejected' навсегда) и с
# пустой причиной в логе, поэтому ловится на старте.
Signal = Annotated[str, AfterValidator(_reject_blank_and_collapse_spacing)]


def _as_group(value: object) -> object:
    """Простое написание — это группа из одного написания.

    Благодаря этому `stack: [yocto, docker]` и `stack: [[yocto], [docker]]` —
    один и тот же конфиг, и профили, написанные до появления групп, не
    переписываются.
    """
    return [value] if isinstance(value, str) else value


# Группа — несколько написаний ОДНОЙ сущности, считающихся одним сигналом
# насыщения (§6). Пустая группа так же бессмысленна, как пустой сигнал.
SignalGroup = Annotated[list[Signal], BeforeValidator(_as_group), Field(min_length=1)]


def _reject_duplicate_signals(groups: list[list[str]]) -> list[list[str]]:
    """Одно и то же написание в списке дважды — всегда опечатка.

    Дубликат накручивает насыщение: `stack: [yocto, yocto, yocto, yocto,
    yocto]` при `saturation.stack = 5` даёт 1.0 на описании с одним словом,
    а `negative: [junior, junior]` — двойной штраф и причину отказа,
    врущую про число различных причин. Мотивация та же, что у
    `_UniqueKeyLoader` для повторного ключа YAML: имя правильное, значение
    законное, а результат — тихо испорченная оценка.

    Сравниваются НОРМАЛИЗОВАННЫЕ формы, потому что дубликатом сигнал
    делает не написание, а регулярка, в которую он компилируется: `1c` и
    `1С` (кириллическая) — один и тот же паттерн. Внутри группы и между
    группами повтор одинаково бессмыслен, поэтому проверка одна на весь
    список.
    """
    seen: dict[str, str] = {}
    for group in groups:
        for signal in group:
            key = normalize(signal)
            first = seen.get(key)
            if first is not None:
                raise ValueError(
                    f"сигнал {signal!r} повторяет {first!r}: это один и тот же паттерн, "
                    "а повтор накручивает насыщение и штраф"
                )
            seen[key] = signal
    return groups


# Список групп. Уникальность проверяется внутри одного списка, а не поперёк
# профиля: `python` законно стоит и в `title_tech`, и в `stack` — это разные
# компоненты формулы с разным насыщением.
SignalGroups = Annotated[list[SignalGroup], AfterValidator(_reject_duplicate_signals)]


class ScheduleConfig(Base):
    interval_hours: int = Field(default=4, ge=1)


class HttpConfig(Base):
    # Нижняя граница паузы — не вкусовая: 0 и отрицательное значение означают
    # обстрел hh.ru без пауз, то есть отказ от вежливости, на которой держится
    # право этого сервиса ходить в источник анонимно.
    delay_between_requests_sec: float = Field(default=1.0, ge=0.1)
    timeout_sec: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=1)
    respect_robots: bool = True


class EnrichConfig(Base):
    # 0 попыток — это вакансия, уходящая в enrich_failed, не будучи ни разу скачана.
    max_attempts: int = Field(default=3, ge=1)


class PathsConfig(Base):
    state: Path
    reports: Path
    logs: Path


class AppConfig(Base):
    # Адрес попадает в User-Agent и служит источнику способом с нами связаться;
    # «не почта вовсе» делает вежливость декоративной.
    contact_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    user_agent: NonEmptyStr
    schedule: ScheduleConfig = ScheduleConfig()
    http: HttpConfig = HttpConfig()
    enrich: EnrichConfig = EnrichConfig()
    # Пустой список приёмников — прогон, который работает и никуда не отчитывается.
    sinks: list[NonEmptyStr] = Field(min_length=1)
    paths: PathsConfig

    @model_validator(mode="after")
    def substitute_contact_email(self) -> "AppConfig":
        """Подставляет адрес в `user_agent`; чужой плейсхолдер — ошибка конфига.

        `str.format` на `hh-search/{version}` бросает `KeyError`, а он не
        `ValueError`: pydantic его не заворачивает, CLI ловит только
        `(OSError, ValueError)`, и пользователь получает голый traceback
        вместо строки «в app.yaml опечатка». Политика проекта — опечатка
        роняет процесс на старте С УКАЗАНИЕМ ПОЛЯ (§7), поэтому отказ
        переводится в обычную ошибку валидации.
        """
        try:
            self.user_agent = self.user_agent.format(contact_email=self.contact_email)
        except (KeyError, IndexError) as error:
            raise ValueError(
                f"user_agent: неизвестный плейсхолдер {error} — поддерживается "
                "только {contact_email}"
            ) from error
        return self


class Weights(Base):
    title: float = Field(ge=0)
    stack: float = Field(ge=0)
    responsibilities: float = Field(ge=0)
    domain: float = Field(ge=0)

    @model_validator(mode="after")
    def check_sum(self) -> "Weights":
        # NaN проходил эту проверку насквозь (любое сравнение с NaN ложно), а
        # отрицательный вес компенсировался положительным до суммы 1.0 —
        # конечность и неотрицательность требуются полями выше.
        total = self.title + self.stack + self.responsibilities + self.domain
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class Saturation(Base):
    # Делитель в скоринге: 0 даёт ZeroDivisionError уже ПОСЛЕ скачивания страницы.
    stack: int = Field(ge=1)
    responsibilities: int = Field(ge=1)


class Signals(Base):
    # Пустой список молча обнуляет свой компонент оценки и не роняет ничего:
    # `stack: []` навсегда держит стек на нуле (потолок 70), `title_roles: []`
    # — заголовок на 0.5 (потолок 80), а все пять пустых дают ровный ноль
    # каждой вакансии и пустой отчёт без единой ошибки в логе. Спека §7
    # обещает обратное: опечатка в конфиге роняет процесс на старте.
    title_roles: SignalGroups = Field(min_length=1)
    title_tech: SignalGroups = Field(min_length=1)
    stack: SignalGroups = Field(min_length=1)
    responsibilities: SignalGroups = Field(min_length=1)
    domain: SignalGroups = Field(min_length=1)


class ProfileConfig(Base):
    weights: Weights
    saturation: Saturation
    # Отрицательный штраф превращает стоп-слово в бонус: «junior» повышал бы
    # балл. Верхняя граница — та же, что у порога и у самой оценки: штраф
    # в 100 очков уже обнуляет вакансию с одним стоп-словом, а всё, что
    # больше, — опечатка (`1500` вместо `15`), которая обнуляет отчёт целиком
    # и никак себя не проявляет. У предела float отказ и вовсе прилетал
    # `ValidationError`'ом изнутри `score()`, то есть после похода в сеть.
    penalty_per_signal: float = Field(ge=0, le=100)
    signals: Signals
    # Единственный список сигналов, которому пустота к лицу: профиль без
    # стоп-слов означает «локально не отсеиваем ничего», и это осмысленно.
    negative: SignalGroups
    report_threshold: float = Field(default=60.0, ge=0, le=100)


# Slug живых курируемых листингов hh.ru — строчные латинские буквы, цифры
# и дефис (`programmist`, `devops`, `1c-programmist`). Список разрешённого
# вместо списка запрещённого выбран сознательно: перечисление запрещённых
# символов (`?&#/ \t\n%`) пропускало `\r`, `\v`, `\f`, `\x00`, `\xa0` и
# unicode-омоглифы (`？`, `∕`), а главное — не запрещало slug'у БЫТЬ
# dot-сегментом (`.`, `..`), из-за чего httpx схлопывал путь и в сеть
# уходил URL, запрещённый живым `Disallow: *?*`. Заодно снимается разбор
# регистра: `Programmist` у hh.ru не существует, и узнавать об этом
# правильно на старте, а не отказом FetchFailed после запроса в сеть.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _reject_url_syntax(value: str) -> str:
    """Slug обязан быть ОДНИМ сегментом пути и ничем больше.

    Он подставляется в `/vacancies/{slug}`, а живой robots.txt hh.ru
    запрещает правилом `Disallow: *?*` любой URL с query-строкой. Slug
    вида `programmist?area=66`, `programmist/../search/vacancy` или просто
    `..` протащил бы запрещённый запрос мимо всех проверок кода — в обход
    договорённости, на которой держится право сервиса ходить в источник.
    Отказ на старте, до первого сетевого запроса.
    """
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"slug {value!r} не похож на slug листинга hh.ru: разрешены строчные "
            "латинские буквы, цифры и дефис (не первым символом) — ровно один "
            "сегмент пути вида /vacancies/{slug}"
        )
    return value


Slug = Annotated[str, Field(min_length=1), AfterValidator(_reject_url_syntax)]


class QuerySpec(Base):
    """Один курируемый листинг hh.ru.

    Параметров RSS (text/area/experience/employment/schedule/period)
    здесь больше нет: они были query-строкой, а query-строка запрещена
    robots.txt. Их удаление намеренно ломает старые конфиги через
    `extra="forbid"` — молча игнорируемое поле хуже отсутствующего,
    потому что создаёт иллюзию работающей фильтрации.
    """

    slug: Slug
    cluster: NonEmptyStr
    weight: int = Field(default=5, ge=0)
    # Верхняя граница — вежливость, а не вкус: одна страница это один
    # запрос к hh.ru, и опечатка `pages: 500` превращает прогон в обстрел.
    pages: int = Field(default=1, ge=1, le=20)


class QueriesConfig(Base):
    queries: list[QuerySpec] = Field(min_length=1)


class Config(Base):
    app: AppConfig
    profile: ProfileConfig
    queries: QueriesConfig
