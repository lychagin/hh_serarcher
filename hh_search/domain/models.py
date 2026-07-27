from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Salary(BaseModel):
    raw: str | None = None
    amount_from: int | None = None
    amount_to: int | None = None
    currency: str | None = None


class DiscoveredVacancy(BaseModel):
    id: str
    url: str
    title: str
    company: str | None = None
    area: str | None = None
    salary: Salary = Salary()
    published_at: datetime
    found_by_query: str


class VacancyDetails(BaseModel):
    description: str
    valid_through: datetime | None = None
    location: str | None = None


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
