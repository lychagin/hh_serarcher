from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleConfig(Base):
    interval_hours: int = 4


class HttpConfig(Base):
    delay_between_requests_sec: float = 1.0
    timeout_sec: float = 20.0
    max_retries: int = 3
    respect_robots: bool = True


class EnrichConfig(Base):
    max_attempts: int = 3


class PathsConfig(Base):
    state: Path
    reports: Path
    logs: Path


class AppConfig(Base):
    contact_email: str
    user_agent: str
    schedule: ScheduleConfig = ScheduleConfig()
    http: HttpConfig = HttpConfig()
    enrich: EnrichConfig = EnrichConfig()
    sinks: list[str]
    paths: PathsConfig

    @model_validator(mode="after")
    def substitute_contact_email(self) -> "AppConfig":
        self.user_agent = self.user_agent.format(contact_email=self.contact_email)
        return self


class Weights(Base):
    title: float
    stack: float
    responsibilities: float
    domain: float

    @model_validator(mode="after")
    def check_sum(self) -> "Weights":
        total = self.title + self.stack + self.responsibilities + self.domain
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class Saturation(Base):
    stack: int
    responsibilities: int


class Signals(Base):
    title_roles: list[str]
    title_tech: list[str]
    stack: list[str]
    responsibilities: list[str]
    domain: list[str]


class ProfileConfig(Base):
    weights: Weights
    saturation: Saturation
    penalty_per_signal: float
    signals: Signals
    negative: list[str]
    report_threshold: float = 60.0


class QueryDefaults(Base):
    experience: list[str] | None = None
    employment: str | None = None
    schedule: str | None = None
    period: int | None = None


class QuerySpec(Base):
    text: str
    cluster: str
    weight: int = 5
    area: list[int] | None = None
    experience: list[str] | None = None
    employment: str | None = None
    schedule: str | None = None
    period: int | None = None


class QueriesConfig(Base):
    defaults: QueryDefaults = QueryDefaults()
    queries: list[QuerySpec]

    @model_validator(mode="after")
    def apply_defaults(self) -> "QueriesConfig":
        for query in self.queries:
            for field in ("experience", "employment", "schedule", "period"):
                if getattr(query, field) is None:
                    setattr(query, field, getattr(self.defaults, field))
        return self


class Config(Base):
    app: AppConfig
    profile: ProfileConfig
    queries: QueriesConfig
