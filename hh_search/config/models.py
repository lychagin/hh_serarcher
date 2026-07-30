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

# Перечисление живёт в domain, не здесь: этот модуль уже зависит от
# domain/models.py транзитивно (см. импорт storage.base ниже), а обратная
# зависимость домена от конфига дала бы цикл импорта. Импорт, а не второе
# объявление — см. докстринг WorkFormat в domain/models.py. `as WorkFormat`
# делает реэкспорт явным: `mypy --strict` запрещает неявный (test_listing.py
# по-прежнему делает `from hh_search.config.models import WorkFormat`).
from hh_search.domain.models import WorkFormat as WorkFormat

# Нормализация берётся у матчера, а не пишется здесь заново: уникальность
# сигналов обязана проверяться в той же форме, в которую они компилируются,
# иначе `yocto` и ` YOCTO ` окажутся «разными» сигналами, будучи одной и
# той же регуляркой. Зависимость односторонняя — matching не знает о конфиге.
from hh_search.filtering.matching import normalize

# Умолчание потолка выборок берётся у слоя, который его исполняет, а не
# пишется здесь вторым числом: разъехавшись, они дали бы конфиг, который
# «ничего не менял», и репозиторий, тихо применяющий другое значение.
from hh_search.storage.base import DEFAULT_BATCH_LIMIT


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


# Домены, зарезервированные стандартом под примеры и документацию:
# RFC 2606 §3 (второй уровень) и RFC 2606 §2 + RFC 6761 (домены верхнего
# уровня). Они не делегированы никому и делегированы не будут, то есть
# письмо по такому адресу не дойдёт ни при каких обстоятельствах.
_RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org", "example.edu"})
_RESERVED_TLDS = frozenset({"example", "invalid", "localhost", "test"})


def _reject_undeliverable_contact(value: str) -> str:
    """Заглушка `your-email@example.com` из образца не имеет права уехать к hh.ru.

    §3.5 называет честный контакт жёстким требованием: `contact_email`
    попадает в `User-Agent` и служит источнику единственным способом
    связаться с нами вместо того, чтобы забанить. Проверка формы адреса
    этого не обеспечивала — заглушка ей соответствует, поэтому первый
    запуск с неотредактированным `app.yaml` завершался `ok`, отправив
    десяток запросов с несуществующим адресом и без единого
    предупреждения.

    Критерий — не «похоже на заглушку», а «домен зарезервирован
    стандартом». Он ловит образец и любую его вариацию, но не спотыкается
    ни об один настоящий адрес, включая адреса на собственных доменах:
    `example@lychagin.dev` и `job@my-example.com` законны, а
    `me@mail.example.com` — нет, потому что поддомен зарезервированного
    домена зарезервирован вместе с ним.
    """
    domain = value.rsplit("@", 1)[-1].strip().rstrip(".").lower()
    labels = domain.split(".")
    registrable = ".".join(labels[-2:])
    if labels[-1] in _RESERVED_TLDS or registrable in _RESERVED_DOMAINS:
        raise ValueError(
            f"адрес {value!r} лежит в домене {domain!r}, зарезервированном под примеры "
            "(RFC 2606, RFC 6761): письмо туда не дойдёт, и hh.ru не сможет с вами "
            "связаться. Заполните contact_email в app.yaml своим настоящим адресом"
        )
    return value


# Адрес попадает в User-Agent и служит источнику способом с нами связаться;
# «не почта вовсе» делает вежливость декоративной, а недоставляемый адрес —
# декоративной так же, просто менее заметно.
ContactEmail = Annotated[
    str,
    Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    AfterValidator(_reject_undeliverable_contact),
]


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


class LimitsConfig(Base):
    """Потолок объёма работы ОДНОГО прогона. Не оптимизация, а предохранитель.

    До него объём не был ограничен ничем. `pages ≤ 20` — вежливость к
    одному листингу, но число листингов не ограничивалось, и произведение
    никто не проверял: конфиг из 50 листингов по 20 страниц принимался
    молча и означал 20 990 запросов к hh.ru и 5.8 часа одних только пауз
    вежливости при `interval_hours: 4` — то есть прогон длиннее интервала
    и демон, работающий встык. Выборки отчёта при этом поднимали в память
    всё подходящее: 50 000 готовых строк — 762 МБ RSS, OOM на VPS с
    гигабайтом.

    Умолчания выбраны так, чтобы штатный режим их не заметил. Замер
    прогона на образцовом конфиге (5 листингов, 9 страниц): 70 запросов —
    9 к листингам при потолке 60 и 60 к страницам вакансий при потолке
    200; в устойчивом режиме, когда почти всё уже обогащено, их около 25.
    Отчёт при этом отдаёт десятки вакансий при потолке в 500 строк.
    """

    # Суммарное число страниц листингов за прогон = число запросов шага
    # discovery. Проверяется как СУММА по всем листингам (см.
    # `Config.check_work_fits_limits`), потому что вежливость измеряется
    # запросами к источнику, а не аккуратностью каждой отдельной записи
    # конфига. Потолок поля — 500: при паузе 1 с это 8 минут одних пауз,
    # и всё, что больше, стоит объявлять сознательно, а не опечаткой.
    listing_pages_per_run: int = Field(default=60, ge=1, le=500)
    # Прямой потолок запросов к странице вакансии за прогон: длина
    # `pending_enrichment` И ЕСТЬ число таких запросов. Бэклог от этого не
    # теряется — выборка отдаёт свежие первыми, остальное берёт следующий
    # прогон.
    enrich_per_run: int = Field(default=200, ge=1)
    # Сколько строк одна выборка поднимает в память. Накрывает `unreported`,
    # `pending_scoring` и `reported_since` — три выборки, читающие колонку
    # `description`, на которую приходится ~80% размера базы.
    rows_per_batch: int = Field(default=DEFAULT_BATCH_LIMIT, ge=1)


class PathsConfig(Base):
    state: Path
    reports: Path
    logs: Path


class AppConfig(Base):
    contact_email: ContactEmail
    user_agent: NonEmptyStr
    schedule: ScheduleConfig = ScheduleConfig()
    http: HttpConfig = HttpConfig()
    enrich: EnrichConfig = EnrichConfig()
    limits: LimitsConfig = LimitsConfig()
    # Пустой список приёмников — прогон, который работает и никуда не отчитывается.
    sinks: list[NonEmptyStr] = Field(min_length=1)
    paths: PathsConfig

    @model_validator(mode="after")
    def check_run_fits_interval(self) -> "AppConfig":
        """Прогон обязан помещаться в интервал между прогонами.

        Считается по нижней границе — по одним лишь паузам вежливости,
        без времени ответа источника и без разбора: `(страницы листингов
        + страницы вакансий) × delay_between_requests_sec`. Даже такая
        оценка ловит настоящую беду: 50 листингов по 20 страниц при
        `interval_hours: 4` дают 5.8 ч только пауз, то есть прогон
        заведомо длиннее интервала, планировщик запускает следующий сразу
        по окончании предыдущего, и вежливая по замыслу служба ходит в
        hh.ru непрерывно. Раньше такой конфиг принимался молча.

        Отказ на старте, а не потолок в рантайме: число запросов целиком
        определяется конфигом, поэтому человеку есть что поправить, и
        узнать об этом он обязан до первого запроса (§7).
        """
        pause_sec = (
            self.limits.listing_pages_per_run + self.limits.enrich_per_run
        ) * self.http.delay_between_requests_sec
        interval_sec = self.schedule.interval_hours * 3600
        if pause_sec > interval_sec:
            raise ValueError(
                f"один прогон не помещается в интервал: потолок "
                f"{self.limits.listing_pages_per_run} + {self.limits.enrich_per_run} запросов "
                f"при паузе {self.http.delay_between_requests_sec} с — это минимум "
                f"{pause_sec / 3600:.1f} ч одних только пауз вежливости, а интервал "
                f"{self.schedule.interval_hours} ч. Демон будет работать встык, без пауз "
                "между прогонами. Уменьшите app.limits или увеличьте schedule.interval_hours"
            )
        return self

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
            # По-русски, как и все прочие сообщения этого сервиса: единственное
            # английское осталось от первой редакции плана и жило дольше всех
            # именно потому, что триаж отложенных minor записал «все сообщения
            # уже по-русски», не проверив исполнением.
            raise ValueError(f"веса обязаны суммироваться в 1.0, получено {total}")
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
    # Не задано — листинг берётся голым путём, как и раньше. Задано — тем же
    # путём с параметром и ОБЯЗАТЕЛЬНЫМ `&page=` (см. build_listing_url:
    # без него URL запрещён robots.txt).
    work_format: WorkFormat | None = None


class QueriesConfig(Base):
    queries: list[QuerySpec] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_slug_and_work_format_pairs(self) -> "QueriesConfig":
        """Одна и та же пара `(slug, work_format)` дважды — всегда опечатка, и тихая.

        Ключ уникальности — пара, а не голый `slug`: второй поток discovery
        (§ конфиг листинга и форма URL) описывает тот же листинг ещё раз, с
        фильтром `work_format`, и это законный конфиг из двух разных
        запросов к hh.ru, а не дубликат. Дубликатом является только полное
        совпадение пары — тогда мотивация прежнего валидатора применяется
        дословно: hh.ru получает одни и те же страницы дважды (при
        `pages: 20` — сорок лишних запросов), `stats.discovered`
        удваивается, а кластер достаётся тому из двух описаний, у которого
        больше `weight`, — то есть человек, аккуратно расписавший два
        разных кластера, получает один и не узнаёт об этом ниоткуда.
        """
        seen: dict[tuple[str, WorkFormat | None], str] = {}
        for query in self.queries:
            key = (query.slug, query.work_format)
            previous = seen.get(key)
            if previous is not None:
                raise ValueError(
                    f"листинг {query.slug!r} с work_format={query.work_format!r} описан дважды "
                    f"(кластеры {previous!r} и {query.cluster!r}): hh.ru получит одни и те же "
                    "страницы по два раза, а кластер достанется тому описанию, у которого "
                    "больше weight"
                )
            seen[key] = query.cluster
        return self

    @property
    def total_pages(self) -> int:
        """Сколько запросов к hh.ru стоит шаг discovery одного прогона."""
        return sum(query.pages for query in self.queries)


class Config(Base):
    app: AppConfig
    profile: ProfileConfig
    queries: QueriesConfig

    @model_validator(mode="after")
    def check_work_fits_limits(self) -> "Config":
        """Сумма страниц по всем листингам — против потолка прогона.

        Проверять `pages` у каждого листинга по отдельности (как делает
        `QuerySpec`, `le=20`) недостаточно и всегда было недостаточно:
        вежливость измеряется числом запросов к источнику, а его даёт
        произведение «листинги × страницы», которое не проверял никто.
        Принятый конфиг из 200 листингов означал 4 000 страниц за прогон.

        Отказ, а не молчаливое усечение: обрезать список листингов
        значило бы, что часть конфига не работает и об этом никто не
        сказал, — тот самый класс тихой потери, против которого написан
        весь проект.
        """
        requested = self.queries.total_pages
        ceiling = self.app.limits.listing_pages_per_run
        if requested > ceiling:
            raise ValueError(
                f"{len(self.queries.queries)} листингов запрашивают суммарно {requested} "
                f"страниц за прогон при потолке {ceiling} "
                f"(app.limits.listing_pages_per_run). Одна страница — один запрос к hh.ru; "
                "уменьшите pages, число листингов либо поднимите потолок сознательно"
            )
        return self
