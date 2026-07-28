from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    discovered: DiscoveredVacancy
    details: VacancyDetails
    score: ScoreBreakdown
    cluster: str
