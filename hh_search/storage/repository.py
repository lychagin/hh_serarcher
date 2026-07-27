import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_REPORTED = "reported"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRepository:
    """Единственное место в проекте, где живёт SQL."""

    def __init__(self, path: Path | str) -> None:
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

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
                                 cluster, cluster_weight, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                vacancy.published_at.isoformat(),
                STATUS_NEW,
                cluster,
                weight,
                _now(),
            ),
        )
        is_new = cursor.rowcount > 0
        self._connection.execute(
            "INSERT OR IGNORE INTO vacancy_query (vacancy_id, query) VALUES (?, ?)",
            (vacancy.id, vacancy.found_by_query),
        )
        if not is_new:
            self._connection.execute(
                "UPDATE vacancy SET cluster = ?, cluster_weight = ? "
                "WHERE id = ? AND cluster_weight < ?",
                (cluster, weight, vacancy.id, weight),
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
            """
            SELECT v.*, (SELECT query FROM vacancy_query q WHERE q.vacancy_id = v.id LIMIT 1)
                   AS found_by_query
            FROM vacancy v
            WHERE v.status = ? AND v.description IS NULL AND v.enrich_attempts < ?
            ORDER BY v.published_at DESC
            """,
            (STATUS_NEW, max_attempts),
        ).fetchall()
        return [self._to_discovered(row) for row in rows]

    def save_details(self, vacancy_id: str, details: VacancyDetails) -> None:
        self._connection.execute(
            "UPDATE vacancy SET description = ?, fetched_at = ? WHERE id = ?",
            (details.description, _now(), vacancy_id),
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

    def save_score(self, vacancy_id: str, score: ScoreBreakdown) -> None:
        self._connection.execute(
            "UPDATE vacancy SET score = ?, score_detail = ? WHERE id = ?",
            (score.total, score.model_dump_json(), vacancy_id),
        )
        self._connection.commit()

    def unreported(self) -> list[ScoredVacancy]:
        rows = self._connection.execute(
            """
            SELECT v.*, (SELECT query FROM vacancy_query q WHERE q.vacancy_id = v.id LIMIT 1)
                   AS found_by_query
            FROM vacancy v
            WHERE v.status = ? AND v.score IS NOT NULL AND v.description IS NOT NULL
            ORDER BY v.score DESC
            """,
            (STATUS_NEW,),
        ).fetchall()
        return [
            ScoredVacancy(
                discovered=self._to_discovered(row),
                details=VacancyDetails(description=row["description"]),
                score=ScoreBreakdown.model_validate(json.loads(row["score_detail"])),
                cluster=row["cluster"] or "",
            )
            for row in rows
        ]

    def mark_reported(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        self._connection.executemany(
            "UPDATE vacancy SET status = ?, reported_at = ? WHERE id = ?",
            [(STATUS_REPORTED, _now(), vacancy_id) for vacancy_id in ids],
        )
        self._connection.commit()

    # --- run journal ---------------------------------------------------

    def start_run(self) -> int:
        cursor = self._connection.execute(
            "INSERT INTO run (started_at, status) VALUES (?, 'running')", (_now(),)
        )
        self._connection.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        status: str,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        allowed = {"discovered", "new_count", "rejected", "enriched", "reported", "error"}
        fields = {name: value for name, value in counters.items() if name in allowed}
        assignments = ", ".join(f"{name} = ?" for name in fields)
        prefix = f"{assignments}, " if assignments else ""
        moment = (finished_at or datetime.now(UTC)).isoformat()
        self._connection.execute(
            f"UPDATE run SET {prefix}status = ?, finished_at = ? WHERE id = ?",
            (*fields.values(), status, moment, run_id),
        )
        self._connection.commit()

    def last_successful_run(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT finished_at FROM run WHERE status IN ('ok', 'partial') "
            "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["finished_at"]) if row else None

    # --- conditional requests ------------------------------------------

    def cache_headers(self, url: str) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        if etag is None and last_modified is None:
            return
        self._connection.execute(
            "INSERT INTO http_cache (url, etag, last_modified, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, "
            "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
            (url, etag, last_modified, _now()),
        )
        self._connection.commit()

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
            published_at=datetime.fromisoformat(row["published_at"]),
            found_by_query=row["found_by_query"] or "",
        )
