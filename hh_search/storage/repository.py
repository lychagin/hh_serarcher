import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from pydantic import ValidationError

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.storage.run_log import RunLog
from hh_search.storage.time_utils import now_iso, parse_utc, to_utc_iso

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_REPORTED = "reported"
# Нечитаемый score_detail (битый JSON или новая обязательная схема
# ScoreBreakdown). Отдельный статус выводит строку из status='new' и тем
# самым из unreported()/pending_enrichment() без удаления — не блокирует
# здоровые вакансии и не спамит лог на каждый прогон.
STATUS_CORRUPT = "corrupt"

logger = logging.getLogger(__name__)


class SqliteRepository:
    """Единственное место в проекте, где живёт SQL.

    Журнал прогонов и HTTP-кэш вынесены в `run_log.RunLog` (тот же
    `sqlite3.Connection`) ради размера файла; инвариант «весь SQL — в
    слое storage» от этого не нарушается.
    """

    def __init__(self, path: Path | str) -> None:
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._run_log = RunLog(self._connection)

    def __enter__(self) -> "SqliteRepository":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def init_schema(self) -> None:
        self._connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._connection.commit()

    # --- discovery -----------------------------------------------------

    def known_ids(self, ids: Iterable[str]) -> set[str]:
        wanted = list(ids)
        if not wanted:
            return set()
        placeholders = ",".join("?" * len(wanted))
        rows = self._connection.execute(
            f"SELECT id FROM vacancy WHERE id IN ({placeholders})", wanted
        )
        return {row["id"] for row in rows}

    def add_discovered(self, vacancy: DiscoveredVacancy, cluster: str, weight: int) -> bool:
        cursor = self._connection.execute(
            """
            INSERT INTO vacancy (id, url, title, company, area, salary_raw, salary_from,
                                 salary_to, salary_currency, published_at, status,
                                 cluster, cluster_weight, primary_query, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                vacancy.id,
                vacancy.url,
                vacancy.title,
                vacancy.company,
                vacancy.area,
                vacancy.salary.raw,
                vacancy.salary.amount_from,
                vacancy.salary.amount_to,
                vacancy.salary.currency,
                to_utc_iso(vacancy.published_at),
                STATUS_NEW,
                cluster,
                weight,
                vacancy.found_by_query,
                now_iso(),
            ),
        )
        is_new = cursor.rowcount > 0
        self._connection.execute(
            "INSERT OR IGNORE INTO vacancy_query (vacancy_id, query, weight) VALUES (?, ?, ?)",
            (vacancy.id, vacancy.found_by_query, weight),
        )
        if not is_new:
            # primary_query переписывается в ТОЙ ЖЕ строке, что и cluster/
            # cluster_weight, — found_by_query в отчёте не может разойтись
            # с кластером, который он же и определил.
            self._connection.execute(
                "UPDATE vacancy SET cluster = ?, cluster_weight = ?, primary_query = ? "
                "WHERE id = ? AND cluster_weight < ?",
                (cluster, weight, vacancy.found_by_query, vacancy.id, weight),
            )
        self._connection.commit()
        return is_new

    def mark_rejected(self, vacancy_id: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE vacancy SET status = ?, reject_reason = ? WHERE id = ?",
            (STATUS_REJECTED, reason, vacancy_id),
        )
        self._connection.commit()

    def set_status(self, vacancy_id: str, status: str) -> None:
        self._connection.execute(
            "UPDATE vacancy SET status = ? WHERE id = ?", (status, vacancy_id)
        )
        self._connection.commit()

    # --- enrichment ----------------------------------------------------

    def pending_enrichment(self, max_attempts: int) -> list[DiscoveredVacancy]:
        rows = self._connection.execute(
            "SELECT * FROM vacancy "
            "WHERE status = ? AND description IS NULL AND enrich_attempts < ? "
            "ORDER BY published_at DESC",
            (STATUS_NEW, max_attempts),
        ).fetchall()
        return [self._to_discovered(row) for row in rows]

    def save_enriched(
        self, vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown
    ) -> None:
        """Единственный способ сохранить обогащение — один UPDATE, одна
        транзакция. Раньше это были save_details + save_score по отдельности;
        крах между ними оставлял вакансию с описанием, но без оценки —
        невидимой ни для pending_enrichment, ни для unreported. Здесь такой
        промежуточной строки не существует: либо записаны обе колонки,
        либо ни одна.
        """
        self._connection.execute(
            "UPDATE vacancy SET description = ?, fetched_at = ?, score = ?, score_detail = ? "
            "WHERE id = ?",
            (details.description, now_iso(), score.total, score.model_dump_json(), vacancy_id),
        )
        self._connection.commit()

    def bump_enrich_attempt(self, vacancy_id: str) -> int:
        self._connection.execute(
            "UPDATE vacancy SET enrich_attempts = enrich_attempts + 1 WHERE id = ?", (vacancy_id,)
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT enrich_attempts FROM vacancy WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return int(row["enrich_attempts"]) if row else 0

    # --- scoring and reporting -----------------------------------------

    def unreported(self) -> list[ScoredVacancy]:
        rows = self._connection.execute(
            "SELECT * FROM vacancy "
            "WHERE status = ? AND score IS NOT NULL AND description IS NOT NULL "
            "ORDER BY score DESC",
            (STATUS_NEW,),
        ).fetchall()
        result: list[ScoredVacancy] = []
        corrupt_ids: list[str] = []
        for row in rows:
            try:
                score = ScoreBreakdown.model_validate(json.loads(row["score_detail"]))
            except (json.JSONDecodeError, ValidationError):
                logger.error(
                    "vacancy %s has unreadable score_detail, marking as %s and skipping",
                    row["id"],
                    STATUS_CORRUPT,
                    exc_info=True,
                )
                corrupt_ids.append(row["id"])
                continue
            result.append(
                ScoredVacancy(
                    discovered=self._to_discovered(row),
                    details=VacancyDetails(description=row["description"]),
                    score=score,
                    cluster=row["cluster"] or "",
                )
            )
        if corrupt_ids:
            self._connection.executemany(
                "UPDATE vacancy SET status = ? WHERE id = ?",
                [(STATUS_CORRUPT, vacancy_id) for vacancy_id in corrupt_ids],
            )
            self._connection.commit()
        return result

    def mark_reported(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        self._connection.executemany(
            "UPDATE vacancy SET status = ?, reported_at = ? WHERE id = ?",
            [(STATUS_REPORTED, now_iso(), vacancy_id) for vacancy_id in ids],
        )
        self._connection.commit()

    # --- run journal and HTTP cache: делегируются в RunLog --------------

    def start_run(self) -> int:
        return self._run_log.start_run()

    def finish_run(
        self,
        run_id: int,
        status: str,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        self._run_log.finish_run(run_id, status, finished_at, **counters)

    def last_successful_run(self) -> datetime | None:
        return self._run_log.last_successful_run()

    def cache_headers(self, url: str) -> dict[str, str]:
        return self._run_log.cache_headers(url)

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self._run_log.save_cache_headers(url, etag, last_modified)

    def reset_cache(self, url: str) -> None:
        self._run_log.reset_cache(url)

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
        return DiscoveredVacancy(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            company=row["company"],
            area=row["area"],
            salary=Salary(
                raw=row["salary_raw"],
                amount_from=row["salary_from"],
                amount_to=row["salary_to"],
                currency=row["salary_currency"],
            ),
            published_at=parse_utc(row["published_at"]),
            found_by_query=row["primary_query"] or "",
        )
