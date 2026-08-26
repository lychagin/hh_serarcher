from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class WorkFormat(StrEnum):
    """Формат работы в терминах hh.ru — значения их перечисления.

    Именно перечисление, а не свободная строка: значение уезжает в
    query-строку запроса к hh.ru, и опечатка обязана падать на старте, а не
    превращаться в бессмысленный фильтр после похода в сеть. Значения взяты
    с живой страницы вакансии (`workFormatsElement`), а не из документации:
    документации на этот ключ нет.

    Живёт в domain, а не в config, хотя первым потребителем (Task 1) был
    именно конфиг (`QuerySpec.work_format`): `config/models.py` уже
    зависит от `domain/models.py` транзитивно, через
    `storage/base.DEFAULT_BATCH_LIMIT`, а обратная зависимость домена от
    конфига дала бы цикл импорта (проверено исполнением: `ImportError:
    cannot import name 'DiscoveredVacancy' from partially initialized
    module`). `config/models.py` импортирует перечисление отсюда, поэтому
    второго объявления по-прежнему нет.
    """

    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ON_SITE = "ON_SITE"
    FIELD_WORK = "FIELD_WORK"


class Salary(BaseModel):
    raw: str | None = None
    amount_from: int | None = None
    amount_to: int | None = None
    currency: str | None = None


class DiscoveredVacancy(BaseModel):
    """То, что известно о вакансии сразу после discovery.

    Листинг hh.ru отдаёт только id, url и заголовок, поэтому company,
    area, salary и published_at здесь необязательны: они заполняются на
    шаге обогащения, со страницы вакансии. `published_at` необязателен
    именно поэтому, а не потому, что дата бывает неизвестна источнику —
    сортировки, опирающиеся на него, обязаны падать на `first_seen_at`.
    """

    id: str
    url: str
    title: str
    company: str | None = None
    area: str | None = None
    salary: Salary = Salary()
    published_at: datetime | None = None
    found_by_query: str


class VacancyDetails(BaseModel):
    """Всё, что даёт страница вакансии: один запрос — один набор полей.

    После переезда discovery на листинг это единственный источник
    компании, региона, зарплаты и даты публикации, поэтому они лежат
    здесь, а не приходят россыпью: их обязана сохранять одна транзакция
    вместе с описанием и оценкой.
    """

    description: str
    valid_through: datetime | None = None
    published_at: datetime | None = None
    company: str | None = None
    area: str | None = None
    salary: Salary = Salary()
    # Множество, не одно значение: живой пример — Team Lead Go из Москвы
    # предлагает ON_SITE, REMOTE и HYBRID одновременно. Правило дальше —
    # «REMOTE присутствует среди форматов», а не «формат равен REMOTE»;
    # обратное выкинуло бы вакансию, которая удалёнку допускает. Пустое
    # множество — блока на странице не нашлось (см. `extract_work_formats`)
    # или вакансия ещё не обогащена; в обоих случаях это не штраф в
    # скоринге (§3 design).
    work_formats: frozenset[WorkFormat] = frozenset()


class ScoreBreakdown(BaseModel):
    # inf/nan запрещены на ВХОДЕ, а не при записи в базу. Причина
    # практическая: json не умеет представлять их иначе как `null`
    # (`model_dump_json()` пишет именно его), а `null` не проходит
    # обратную валидацию при чтении. Оценка с inf оказалась бы записью,
    # которую нельзя прочитать: вакансия вечно ходила бы по кругу
    # pending_scoring -> unreported -> карантин -> pending_scoring, в
    # отчёт не попадая никогда и заливая лог ERROR каждый прогон.
    # Достижимо без порчи базы — опечаткой в YAML (`penalty: 1e400`),
    # поэтому отказ обязан быть громким и в момент вычисления оценки.
    model_config = ConfigDict(allow_inf_nan=False)

    title: float
    stack: float
    responsibilities: float
    domain: float
    penalty: float
    total: float
    matched: dict[str, list[str]] = {}


class ScoredVacancy(BaseModel):
    """Вакансия, готовая к отправке.

    В базе не хранится: собирается из колонок при чтении (`storage/mappers.py`),
    поэтому новое поле здесь не трогает ни одной уже записанной строки — в
    отличие от `ScoreBreakdown`, который уезжает в `score_detail` как JSON.
    """

    discovered: DiscoveredVacancy
    details: VacancyDetails
    score: ScoreBreakdown
    cluster: str
    # Косинус между вектором описания и вектором профиля (§6 спеки
    # `2026-08-26-local-llm-design.md`). Разрывает связки одинаковых
    # ключевых оценок и НЕ участвует в `score.total`: замер на всех 573
    # описаниях показал, что весом он тянет наверх почти половину отсева.
    #
    # `None`, а не `0.0`, и это не педантизм. «Не считалось» — модель
    # недоступна, `llm.semantic: false`, вектор снят уборкой описаний —
    # и «посчиталось, вышло мало» суть разные вещи. Ноль на месте
    # неизвестности утверждал бы, что вакансия далека от профиля, тогда
    # как о ней просто нет данных, и отправлял бы её вниз связки на этом
    # основании.
    semantic: float | None = None
