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
from hh_search.storage.mappers import to_discovered, to_id_and_title, to_scored, to_scoring_task
from hh_search.storage.migrations import apply_schema
from hh_search.storage.quarantine import Quarantine, safe_rows
from hh_search.storage.run_log import RunLog
from hh_search.storage.time_utils import now_iso, to_utc_iso, to_utc_iso_optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_REPORTED = "reported"

# Машинные коды причины отказа — колонка `reject_code`, отдельная от
# человекочитаемого `reject_reason`. Разведены они не ради красоты:
# отказ префильтра обратим (решение о нём локальное, и правка конфига
# обязана его отменять), а исчерпание попыток скачивания — нет. Выбирать
# обратимые строки разбором текста причины нельзя: текст префильтра
# перечисляет совпавшие стоп-слова и будет меняться, а разъехавшийся
# префикс молча изменил бы множество возвращаемых вакансий — ровно тот
# класс тихого отказа, против которого написан весь остальной модуль.
REJECT_CODE_PREFILTER = "prefilter"
REJECT_CODE_ENRICH_FAILED = "enrich_failed"

# Текст причины при исчерпании попыток скачивания (спека §5.2). Ставится
# внутри `bump_enrich_attempt`, тем же UPDATE, что и счётчик, и совпадает
# с кодом: этому отказу нечего добавить к коду, тогда как причина
# префильтра перечисляет слова и потому от кода отличается.
REJECT_ENRICH_FAILED = REJECT_CODE_ENRICH_FAILED

logger = logging.getLogger(__name__)

# Колонки, из которых строится DiscoveredVacancy. Обёрнуты в CAST(... AS
# BLOB) ВСЕ до единой: sqlite3 декодирует TEXT-значение на этапе fetch,
# до того как код увидит хоть одну строку, — битые байты в любой колонке
# роняют ВЕСЬ курсор. Числовые не исключение: SQLite типизирован
# динамически, в INTEGER-колонке может лежать текст. BLOB отдаёт сырые
# байты, разбор переезжает в mappers, где его ловит safe_rows. `id`
# обёрнут наравне с остальными: иначе испорченный первичный ключ
# навсегда убивает и очередь, и отчёт, а так карантин адресует строку
# через WHERE CAST(id AS BLOB) = ?.
_DISCOVERED_COLUMNS_SQL = (
    "CAST(id AS BLOB) AS id, CAST(url AS BLOB) AS url, CAST(title AS BLOB) AS title, "
    "CAST(company AS BLOB) AS company, CAST(area AS BLOB) AS area, "
    "CAST(salary_raw AS BLOB) AS salary_raw, CAST(salary_from AS BLOB) AS salary_from, "
    "CAST(salary_to AS BLOB) AS salary_to, "
    "CAST(salary_currency AS BLOB) AS salary_currency, "
    "CAST(published_at AS BLOB) AS published_at, "
    "CAST(primary_query AS BLOB) AS primary_query"
)


class SqliteRepository:
    """Единственное место в проекте, где живёт SQL.

    Для `status = 'new'` определены три непересекающиеся выборки, вместе
    покрывающие ВСЕ состояния без исключений:
    `pending_enrichment` (описания нет — надо в сеть), `pending_scoring`
    (описание есть, оценки нет — надо пересчитать локально) и
    `unreported` (заполнено обе — готово к отправке). Отсюда инвариант
    модуля: раз записанное `description` не обнуляет ни одна выборка и
    ни один путь обработки порчи, поэтому страница вакансии скачивается
    не более одного раза за всю жизнь. Исчерпание попыток скачивания не
    создаёт четвёртого, невидимого состояния: лимит применяется внутри
    `bump_enrich_attempt` тем же оператором, что и инкремент, и строка
    сразу становится терминальной (`rejected` / `enrich_failed`).

    Отказ ПРЕФИЛЬТРА при этом терминальным больше не является. Решение о
    нём чисто локальное — заголовок уже лежит в базе, сеть не нужна, —
    поэтому опечатка в списке стоп-слов не имеет права стоить вакансий
    навсегда. `rejected_by_prefilter()` + `requeue_prefiltered()`
    возвращают такие строки в `new` с обнулённым счётчиком попыток, то
    есть прямо в `pending_enrichment`; три выборки от этого не
    пересекаются (см. `requeue_prefiltered`). Отличается обратимый отказ
    от необратимого машинным кодом `reject_code`, а не разбором текста
    причины.

    Журнал прогонов и HTTP-кэш вынесены в `run_log.RunLog` (тот же
    `sqlite3.Connection`) ради размера файла; инвариант «весь SQL — в
    слое storage» от этого не нарушается.
    """

    def __init__(self, path: Path | str) -> None:
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._run_log = RunLog(self._connection)
        self._quarantine = Quarantine(self._connection)

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
        """Создать схему и догнать существующую базу до неё.

        Второе обязательно: база персистентна, а `CREATE TABLE IF NOT
        EXISTS` на уже существующей таблице не добавляет новых колонок.
        """
        apply_schema(self._connection, SCHEMA_PATH.read_text(encoding="utf-8"))

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
                to_utc_iso_optional(vacancy.published_at),
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

    def mark_rejected(self, vacancy_id: str, reason: str, code: str) -> None:
        """Отказ: человекочитаемая причина и машинный код — одним UPDATE.

        `code` обязателен и без умолчания: отказ без кода необратим по
        построению (вернуть его нечему), и молча выбирать такой исход за
        вызывающего этот метод права не имеет.
        """
        self._connection.execute(
            "UPDATE vacancy SET status = ?, reject_reason = ?, reject_code = ? WHERE id = ?",
            (STATUS_REJECTED, reason, code, vacancy_id),
        )
        self._connection.commit()

    def set_status(self, vacancy_id: str, status: str) -> bool:
        """Ручная смена статуса. `False` — такой вакансии в базе нет.

        Результат возвращается, потому что вызывающий — CLI, а id туда
        вводит человек: `mark 999999 applied` на несуществующей вакансии
        без этого печатал бы «готово» и отдавал код 0.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET status = ? WHERE id = ?", (status, vacancy_id)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    # --- 1: обогащение, единственная выборка, ходящая в сеть -------------

    def pending_enrichment(self, max_attempts: int) -> list[DiscoveredVacancy]:
        """Очередь обогащения — единственная выборка, ходящая в сеть.

        `CAST(COALESCE(enrich_attempts, 0) AS INTEGER)`, а не голая
        колонка: у SQLite типы динамические, и текст в этой колонке
        сравнивается с числом по правилу «любое число меньше любого
        текста», то есть предикат становится ложным навсегда. Вакансия
        при этом невидима ВСЕМ трём выборкам разом (`pending_scoring` и
        `unreported` требуют описания, которого у неё нет) и пропадает
        молча — единственный вид порчи, который `safe_rows` поймать не
        может в принципе: он защищает разбор строк, а здесь строка не
        доходит до разбора. `CAST` возвращает такую строку в очередь, а
        первый же `bump_enrich_attempt` делает счётчик снова числом,
        поэтому вечного цикла из этого не выходит.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL} FROM vacancy "
            "WHERE status = ? AND description IS NULL "
            "AND CAST(COALESCE(enrich_attempts, 0) AS INTEGER) < ? "
            "ORDER BY COALESCE(published_at, first_seen_at) DESC",
            (STATUS_NEW, max_attempts),
        ).fetchall()
        return safe_rows(rows, to_discovered, self._quarantine)

    # Поля, которые приносит ОДНА скачанная страница вакансии. После
    # переезда discovery на листинг это единственный их источник, поэтому
    # они пишутся тем же оператором, что описание и оценка: разъехаться
    # описанию и компании, за которые заплачено одним запросом, нечем.
    #
    # COALESCE(:поле, поле) — «заполнить, но не стереть». Страница может
    # честно не содержать зарплаты («не указана» — самый обычный случай),
    # и присвоение NULL затирало бы значение, добытое РАНЬШЕ: у баз,
    # мигрировавших с RSS, company/area/salary/published_at уже заполнены
    # с шага discovery. Обесценить их отсутствием блока на странице —
    # чистая потеря. Обратной опасности нет: описание скачивается ровно
    # один раз за жизнь вакансии, поэтому «залипнуть» устаревшему
    # значению обогащения неоткуда.
    _ENRICHED_COLUMNS_SQL = (
        "description = :description, fetched_at = :fetched_at, "
        "published_at = COALESCE(:published_at, published_at), "
        "valid_through = COALESCE(:valid_through, valid_through), "
        "company = COALESCE(:company, company), "
        "area = COALESCE(:area, area), "
        "salary_raw = COALESCE(:salary_raw, salary_raw), "
        "salary_from = COALESCE(:salary_from, salary_from), "
        "salary_to = COALESCE(:salary_to, salary_to), "
        "salary_currency = COALESCE(:salary_currency, salary_currency)"
    )

    @staticmethod
    def _enriched_params(vacancy_id: str, details: VacancyDetails) -> dict[str, object]:
        return {
            "id": vacancy_id,
            "description": details.description,
            "fetched_at": now_iso(),
            "published_at": to_utc_iso_optional(details.published_at),
            "valid_through": to_utc_iso_optional(details.valid_through),
            "company": details.company,
            "area": details.area,
            "salary_raw": details.salary.raw,
            "salary_from": details.salary.amount_from,
            "salary_to": details.salary.amount_to,
            "salary_currency": details.salary.currency,
        }

    def save_description(self, vacancy_id: str, details: VacancyDetails) -> None:
        """Сохранить страницу без оценки: она скачана, оценки ещё нет.

        Отдельный примитив нужен конвейеру для случая «скоринг бросил
        исключение»: страница уже стоила одного запроса к hh.ru, и терять
        её из-за ошибки чисто локального вычисления нельзя — иначе
        следующий прогон снова пойдёт в сеть за той же страницей.
        Вакансия остаётся в `pending_scoring` и досчитывается локально.
        Компания, регион, зарплата и даты сохраняются здесь наравне с
        описанием ровно по той же причине.
        """
        self._connection.execute(
            f"UPDATE vacancy SET {self._ENRICHED_COLUMNS_SQL} WHERE id = :id",
            self._enriched_params(vacancy_id, details),
        )
        self._connection.commit()

    def save_enriched(
        self, vacancy_id: str, details: VacancyDetails, score: ScoreBreakdown
    ) -> None:
        """Вся страница и оценка одним UPDATE — обычный путь после скачивания.

        Одним оператором пишется ВСЁ, что принёс единственный запрос к
        hh.ru: описание, компания, регион, зарплата, даты — и оценка,
        посчитанная по ним же. Разъехаться им нечем по построению.

        Сериализация вынесена ИЗ параметров сознательно: рядом с ними она
        вычисляется до `UPDATE`, поэтому её отказ (например
        `PydanticSerializationError`, подкласс `ValueError`) выбрасывал
        вместе с оценкой уже скачанную страницу — и следующий прогон шёл
        за ней в сеть повторно. Теперь неудача сериализации сохраняет
        страницу без оценки и пробрасывает ошибку: вакансия попадает в
        `pending_scoring`, страница не перекачивается.
        """
        try:
            score_detail = score.model_dump_json()
        except ValueError:
            self.save_description(vacancy_id, details)
            raise
        params = self._enriched_params(vacancy_id, details)
        params["score"] = score.total
        params["score_detail"] = score_detail
        self._connection.execute(
            f"UPDATE vacancy SET {self._ENRICHED_COLUMNS_SQL}, "
            "score = :score, score_detail = :score_detail WHERE id = :id",
            params,
        )
        self._connection.commit()

    def bump_enrich_attempt(self, vacancy_id: str, max_attempts: int) -> int:
        """Инкремент счётчика и, при исчерпании лимита, отказ — ОДНИМ UPDATE.

        Лимит живёт здесь, а не в конвейере, по той же причине, по которой
        описание и оценка пишутся одним оператором. Пока это были два
        отдельно закоммиченных состояния (`bump_enrich_attempt`, затем
        `mark_rejected`), между ними существовало состояние
        «`status = 'new'`, `enrich_attempts >= max`», невидимое НИ ОДНОЙ
        из трёх выборок: `pending_enrichment` отсекает такую строку по
        счётчику, а `pending_scoring`/`unreported` — по пустому описанию.
        Вакансия пропадала навсегда, причём без всякой аварии: достаточно
        было, чтобы конвейер не дошёл до второго вызова. Теперь это
        состояние недостижимо по построению, а не по дисциплине
        вызывающего.

        Статус меняется только у строки со `status = 'new'`: терминальные
        `corrupt`/`reported` не воскрешаются и не переписываются.
        """
        self._connection.execute(
            "UPDATE vacancy SET enrich_attempts = enrich_attempts + 1, "
            "status = CASE WHEN enrich_attempts + 1 >= :limit AND status = :new "
            "THEN :rejected ELSE status END, "
            "reject_reason = CASE WHEN enrich_attempts + 1 >= :limit AND status = :new "
            "THEN :reason ELSE reject_reason END, "
            "reject_code = CASE WHEN enrich_attempts + 1 >= :limit AND status = :new "
            "THEN :code ELSE reject_code END "
            "WHERE id = :id",
            {
                "limit": max_attempts,
                "new": STATUS_NEW,
                "rejected": STATUS_REJECTED,
                "reason": REJECT_ENRICH_FAILED,
                "code": REJECT_CODE_ENRICH_FAILED,
                "id": vacancy_id,
            },
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT enrich_attempts FROM vacancy WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return int(row["enrich_attempts"]) if row else 0

    # --- возврат из отказа префильтра, сеть не задействуется -------------

    def rejected_by_prefilter(self) -> list[tuple[str, str]]:
        """`(id, заголовок)` строк, отбракованных ПРЕФИЛЬТРОМ.

        Отбор идёт по машинному коду `reject_code`, а не по тексту
        `reject_reason`: текст причины принадлежит человеку и будет
        меняться, и молчаливо разъехавшийся префикс изменил бы множество
        возвращаемых вакансий без единого признака. `enrich_failed`
        сюда не попадает по построению — у него другой код, и другого
        смысла отказ: страница не разбирается, и заголовок об этом
        ничего не знает.

        Читается ровно то, чем располагает решение префильтра, — id и
        заголовок. Компания, зарплата и описание не выбираются вовсе: их
        порча не имеет права трогать вакансию, судьба которой решается
        одним заголовком.
        """
        rows = self._connection.execute(
            "SELECT CAST(id AS BLOB) AS id, CAST(title AS BLOB) AS title FROM vacancy "
            "WHERE status = ? AND reject_code = ?",
            (STATUS_REJECTED, REJECT_CODE_PREFILTER),
        ).fetchall()
        return safe_rows(rows, to_id_and_title, self._quarantine)

    def requeue_prefiltered(self, ids: Sequence[str]) -> int:
        """Вернуть отбракованные префильтром вакансии в очередь. Одна транзакция.

        `executemany` + один `commit()` — это ОДНА транзакция sqlite3:
        смерть процесса посреди возврата не оставляет половины строк
        возвращёнными. Рваного состояния нет и внутри строки: статус,
        причина, код и счётчик попыток меняются одним оператором.

        `enrich_attempts = 0` здесь обязателен, а не косметичен. Вакансия
        могла израсходовать попытки скачивания ДО того, как правка
        конфига её отбраковала, и возврат в `new` с исчерпанным счётчиком
        воспроизвёл бы Critical спеки §5.2: `status = 'new'`,
        `description IS NULL`, `enrich_attempts >= max` — состояние,
        невидимое ВСЕМ трём выборкам. Плата названа честно: возврат даёт
        странице заново полный бюджет попыток, то есть при живом 404
        стоит до `max_attempts` запросов. Платится она только за
        осознанную правку конфига и ровно один раз: строка возвращается,
        пока совпадает условие, а после возврата условия больше нет.

        WHERE-охрана повторяет предикат выборки, поэтому метод
        идемпотентен и не воскрешает ничего чужого: ни `enrich_failed`,
        ни `corrupt`, ни `reported`, ни отказ, поставленный человеком
        через CLI (у него `reject_code IS NULL`).
        """
        if not ids:
            return 0
        cursor = self._connection.executemany(
            "UPDATE vacancy SET status = :new, reject_reason = NULL, reject_code = NULL, "
            "enrich_attempts = 0 "
            "WHERE id = :id AND status = :rejected AND reject_code = :code",
            [
                {
                    "new": STATUS_NEW,
                    "rejected": STATUS_REJECTED,
                    "code": REJECT_CODE_PREFILTER,
                    "id": vacancy_id,
                }
                for vacancy_id in ids
            ],
        )
        self._connection.commit()
        return int(cursor.rowcount)

    # --- 2: пересчёт оценки, сеть не задействуется -----------------------

    def pending_scoring(self) -> list[tuple[DiscoveredVacancy, VacancyDetails]]:
        """Описание есть, оценки нет: пересчитать локально.

        Ровно та щель, через которую вакансия раньше проваливалась мимо
        обеих выборок, — теперь это явное состояние со своей очередью, а
        не повод идти за уже скачанной страницей второй раз.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL "
            "AND score_detail IS NULL ORDER BY COALESCE(published_at, first_seen_at) DESC",
            (STATUS_NEW,),
        ).fetchall()
        return safe_rows(rows, to_scoring_task, self._quarantine)

    def save_score(self, vacancy_id: str, score: ScoreBreakdown) -> None:
        """Записать пересчитанную оценку, не трогая описание."""
        self._connection.execute(
            "UPDATE vacancy SET score = ?, score_detail = ? WHERE id = ?",
            (score.total, score.model_dump_json(), vacancy_id),
        )
        self._connection.commit()

    # --- 3: отчёт --------------------------------------------------------

    def unreported(self) -> list[ScoredVacancy]:
        self._warn_about_unscored()
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL "
            "AND score_detail IS NOT NULL ORDER BY score DESC",
            (STATUS_NEW,),
        ).fetchall()
        return safe_rows(rows, to_scored, self._quarantine)

    def reported_since(self, cutoff: datetime) -> list[ScoredVacancy]:
        """Уже отправленное — для повторной генерации отчёта командой `report`.

        Читается ровно теми же средствами, что `unreported()`:
        `CAST(... AS BLOB)` плюс `safe_rows`. Без них одна испорченная
        строка роняла бы весь курсор (sqlite3 декодирует TEXT на этапе
        fetch, до того как код увидит хоть одну строку) — то есть
        единственный способ пользователя вернуть историю ломался бы от
        того, что конвейер переживает. Запрос, которым вакансия найдена,
        берётся из колонки `primary_query`, а не подзапросом по
        `vacancy_query`: подзапрос без ORDER BY недетерминирован и мог
        разойтись с кластером в том же отчёте.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail "
            "FROM vacancy WHERE status = ? AND reported_at >= ? "
            "AND description IS NOT NULL AND score_detail IS NOT NULL ORDER BY score DESC",
            (STATUS_REPORTED, to_utc_iso(cutoff)),
        ).fetchall()
        return safe_rows(rows, to_scored, self._quarantine)

    def _warn_about_unscored(self) -> None:
        """Сторож очереди пересчёта: молчаливого пропуска быть не должно.

        Хранилище не может заставить конвейер вызвать `pending_scoring()`
        перед отчётом, но может не дать пропуску пройти незамеченным.
        Каждая такая строка — вакансия, которая уже стоила запроса к
        hh.ru и всё равно не попадёт ни в один отчёт, пока очередь
        пересчёта не будет обработана. COUNT(*) по трём предикатам не
        декодирует ни одного значения, поэтому сторож не падает даже на
        полностью испорченной таблице.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS stuck FROM vacancy WHERE status = ? "
            "AND description IS NOT NULL AND score_detail IS NULL",
            (STATUS_NEW,),
        ).fetchone()
        stuck = int(row["stuck"]) if row else 0
        if stuck:
            logger.error(
                "%d вакансий с готовым описанием и без оценки не попадут в отчёт: "
                "конвейер не вызвал pending_scoring() перед unreported(). Описание "
                "у них есть, перекачка не нужна — нужен локальный пересчёт оценки",
                stuck,
            )

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
        *,
        finished_at: datetime | None = None,
        **counters: int | str | None,
    ) -> None:
        self._run_log.finish_run(run_id, status, finished_at=finished_at, **counters)

    def last_successful_run(self) -> datetime | None:
        return self._run_log.last_successful_run()

    def cache_headers(self, url: str) -> dict[str, str]:
        return self._run_log.cache_headers(url)

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self._run_log.save_cache_headers(url, etag, last_modified)

    def reset_cache(self, url: str) -> None:
        self._run_log.reset_cache(url)
