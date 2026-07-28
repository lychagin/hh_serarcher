from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


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


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("пустой или пробельный сигнал недопустим: он совпадёт с любым текстом")
    return value


# Пустой сигнал компилируется в регулярку из одних границ слова и матчит почти
# любой заголовок. В отсеве это необратимо (status='rejected' навсегда) и с
# пустой причиной в логе, поэтому ловится на старте.
Signal = Annotated[str, AfterValidator(_reject_blank)]


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
        self.user_agent = self.user_agent.format(contact_email=self.contact_email)
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
    title_roles: list[Signal]
    title_tech: list[Signal]
    stack: list[Signal]
    responsibilities: list[Signal]
    domain: list[Signal]


class ProfileConfig(Base):
    weights: Weights
    saturation: Saturation
    # Отрицательный штраф превращает стоп-слово в бонус: «junior» повышал бы балл.
    penalty_per_signal: float = Field(ge=0)
    signals: Signals
    negative: list[Signal]
    report_threshold: float = Field(default=60.0, ge=0, le=100)


def _reject_url_syntax(value: str) -> str:
    """Slug обязан быть ОДНИМ сегментом пути и ничем больше.

    Он подставляется в `/vacancies/{slug}`, а живой robots.txt hh.ru
    запрещает правилом `Disallow: *?*` любой URL с query-строкой. Slug
    вида `programmist?area=66` или `programmist/../search/vacancy`
    протащил бы запрещённый запрос мимо всех проверок кода — не в обход
    матчера robots (он такой URL поймает), а в обход договорённости, на
    которой держится право сервиса ходить в источник. Отказ на старте,
    до первого сетевого запроса.
    """
    if value.strip() != value:
        raise ValueError("slug не может начинаться или заканчиваться пробелом")
    forbidden = set(value) & set("?&#/ \t\n%")
    if forbidden:
        raise ValueError(
            f"slug {value!r} содержит недопустимые символы {sorted(forbidden)}: "
            "разрешён ровно один сегмент пути вида /vacancies/{slug}"
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
