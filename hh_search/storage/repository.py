import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from hh_search.domain.models import (
    DiscoveredVacancy,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
)
from hh_search.storage.mappers import decode_optional_text, decode_text, to_discovered
from hh_search.storage.run_log import RunLog
from hh_search.storage.time_utils import now_iso, to_utc_iso

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_REPORTED = "reported"
# Терминальный статус: вакансия исчерпала MAX_CORRUPT_ATTEMPTS попыток
# самовосстановления (см. _quarantine) и больше не возвращается в очередь.
STATUS_CORRUPT = "corrupt"

# Порог попыток карантина на одну вакансию, после которого self-heal
# сменяется терминальным статусом — без него системная порча (например,
# несовместимая после эволюции схема ScoreBreakdown) вечно перекачивает
# и переоценивает весь бэклог.
MAX_CORRUPT_ATTEMPTS = 3

# Колонки, из которых строится DiscoveredVacancy. Текстовые обёрнуты в
# CAST(... AS BLOB): sqlite3 декодирует TEXT-колонки в UTF-8 на этапе
# fetch, до того как код увидит хоть одну строку, — битые байты в любой
# из них роняют ВЕСЬ курсор, а не одну строку. BLOB отдаёт сырые байты,
# decode переносится в mappers.to_discovered, где его можно поймать
# точечно per-row.
_DISCOVERED_COLUMNS_SQL = (
    "id, CAST(url AS BLOB) AS url, CAST(title AS BLOB) AS title, "
    "CAST(company AS BLOB) AS company, CAST(area AS BLOB) AS area, "
    "CAST(salary_raw AS BLOB) AS salary_raw, salary_from, salary_to, "
    "CAST(salary_currency AS BLOB) AS salary_currency, "
    "CAST(published_at AS BLOB) AS published_at, "
    "CAST(primary_query AS BLOB) AS primary_query"
)

# TypeError — json.loads(None) при score_detail = NULL. ValueError —
# общий предок JSONDecodeError, UnicodeDecodeError и pydantic
# ValidationError (все три — его подклассы), т.е. любая ожидаемая форма
# порчи данных. Не Exception: ошибка в самом коде (например, опечатка в
# ScoreBreakdown.model_validate) не является порчей данных и обязана
# упасть громко, а не тихо закарантинить здоровую вакансию.
_CORRUPTION_EXCEPTIONS = (TypeError, ValueError)

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
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, corrupt_count FROM vacancy "
            "WHERE status = ? AND description IS NULL AND enrich_attempts < ? "
            "ORDER BY published_at DESC",
            (STATUS_NEW, max_attempts),
        ).fetchall()
        result: list[DiscoveredVacancy] = []
        for row in rows:
            try:
                result.append(to_discovered(row))
            except _CORRUPTION_EXCEPTIONS:
                logger.error(
                    "вакансия %s: данные обнаружения повреждены", row["id"], exc_info=True
                )
                self._quarantine(row["id"], int(row["corrupt_count"]), payload=None)
        return result

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
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail, "
            "corrupt_count FROM vacancy "
            "WHERE status = ? AND score IS NOT NULL AND description IS NOT NULL "
            "ORDER BY score DESC",
            (STATUS_NEW,),
        ).fetchall()
        result: list[ScoredVacancy] = []
        for row in rows:
            raw_score_detail = row["score_detail"]
            try:
                text = decode_optional_text(raw_score_detail)
                if text is None:
                    raise TypeError("score_detail is NULL")
                score = ScoreBreakdown.model_validate(json.loads(text))
                result.append(
                    ScoredVacancy(
                        discovered=to_discovered(row),
                        details=VacancyDetails(description=decode_text(row["description"])),
                        score=score,
                        cluster=decode_optional_text(row["cluster"]) or "",
                    )
                )
            except _CORRUPTION_EXCEPTIONS:
                logger.error(
                    "вакансия %s: данные повреждены, отправляем в карантин",
                    row["id"],
                    exc_info=True,
                )
                self._quarantine(row["id"], int(row["corrupt_count"]), raw_score_detail)
        return result

    def _quarantine(
        self, vacancy_id: str, previous_corrupt_count: int, payload: bytes | None
    ) -> None:
        """Карантин — самовосстановление, ограниченное MAX_CORRUPT_ATTEMPTS.

        До порога: description/score/score_detail обнуляются, enrich_attempts
        сбрасывается (иначе вакансия могла упереться в max_attempts из-за
        старых неудач скачивания, не связанных с этой порчей), status
        остаётся 'new' — вакансия сама возвращается в pending_enrichment.
        По достижении порога — терминальный STATUS_CORRUPT, из очереди
        выведена насовсем, с единственной записью в лог. В обоих случаях
        исходный payload сохраняется в corrupt_payload, а не теряется.
        """
        new_count = previous_corrupt_count + 1
        if new_count >= MAX_CORRUPT_ATTEMPTS:
            logger.error(
                "вакансия %s: превышен лимит восстановлений (%s из %s), "
                "переводим в терминальный статус %s",
                vacancy_id,
                new_count,
                MAX_CORRUPT_ATTEMPTS,
                STATUS_CORRUPT,
            )
            self._connection.execute(
                "UPDATE vacancy SET status = ?, description = NULL, score = NULL, "
                "score_detail = NULL, enrich_attempts = 0, corrupt_count = ?, "
                "corrupt_payload = ? WHERE id = ?",
                (STATUS_CORRUPT, new_count, payload, vacancy_id),
            )
        else:
            self._connection.execute(
                "UPDATE vacancy SET description = NULL, score = NULL, score_detail = NULL, "
                "enrich_attempts = 0, corrupt_count = ?, corrupt_payload = ? WHERE id = ?",
                (new_count, payload, vacancy_id),
            )
        self._connection.commit()

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
