import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from pydantic import ValidationError

from hh_search.domain.models import (
    DiscoveredVacancy,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
    VacancyFacts,
)
from hh_search.storage.base import (
    DEFAULT_BATCH_LIMIT,
    REJECT_CODE_ENRICH_FAILED,
    REJECT_CODE_PREFILTER,
    REJECT_ENRICH_FAILED,
    STATUS_NEW,
    STATUS_REJECTED,
    STATUS_REPORTED,
)
from hh_search.storage.mappers import (
    decode_text,
    to_discovered,
    to_embedding_task,
    to_facts_task,
    to_id_and_title,
    to_scored,
    to_scoring_task,
)
from hh_search.storage.migrations import apply_schema
from hh_search.storage.quarantine import Quarantine, safe_rows
from hh_search.storage.retention import Retention
from hh_search.storage.run_log import RunLog, RunSummary
from hh_search.storage.time_utils import now_iso, to_utc_iso, to_utc_iso_optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Коды и статусы переехали в `storage/base.py`, к протоколу: их называет
# конвейер, и импортировать ради константы модуль, в котором живёт SQL,
# значит держать в нём зависимость от реализации хранилища. Здесь они
# перечислены заново только затем, чтобы прежние импорты
# `from ...repository import REJECT_CODE_PREFILTER` не сломались.
__all__ = [
    "REJECT_CODE_ENRICH_FAILED",
    "REJECT_CODE_PREFILTER",
    "REJECT_ENRICH_FAILED",
    "SCHEMA_PATH",
    "STATUS_NEW",
    "STATUS_REJECTED",
    "STATUS_REPORTED",
    "SqliteRepository",
]

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
        self._retention = Retention(self._connection)

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

        Меняется не только колонка `status`, и это не удобство. Смена
        ОДНОГО столбца на `new` воссоздавала на терминальной строке ровно
        то состояние, ради недостижимости которого переписан
        `bump_enrich_attempt`: `status='new'`, `description IS NULL`,
        `enrich_attempts >= max` — невидимое ВСЕМ трём выборкам. А
        `mark <id> new` — это ровно то, что напишет человек, желающий
        «попробовать ещё раз»: команда отвечала успехом, следующий прогон
        рапортовал `ok`, и вакансия исчезала навсегда. Поэтому возврат в
        `new` обнуляет счётчик попыток тем же UPDATE, что и статус, — по
        образцу `requeue_prefiltered`.

        Машинная причина отказа стирается при ЛЮБОМ ручном статусе:
        решение человека отменяет решение машины, а не сосуществует с
        ним. Иначе `mark X new` на отказе префильтра оставлял
        `reject_code='prefilter'`, и следующий `mark X rejected`
        возвращался в очередь ближайшим прогоном — вопреки спеке §5.2,
        где ручной отказ кода не имеет вовсе и не возвращается.

        `reported_at` при переводе в `reported` проставляется тем же
        UPDATE — по той же причине и тем же приёмом. Без него `mark X
        reported` создавал ещё одно невидимое состояние:
        `status='reported'` уводит строку из `unreported()` (там
        `status='new'`), а пустой `reported_at` — из `reported_since()`
        (там `reported_at >= ?`). Вакансия пропадала из ОБОИХ путей
        отчёта, а команда отвечала «1 → reported» и кодом 0. Ставится
        только если его ещё нет: время первой отправки — история, и
        затирать её ручной командой незачем.
        """
        cursor = self._connection.execute(
            "UPDATE vacancy SET status = :status, reject_reason = NULL, reject_code = NULL, "
            "enrich_attempts = CASE WHEN :status = :new THEN 0 ELSE enrich_attempts END, "
            "reported_at = CASE WHEN :status = :reported THEN COALESCE(reported_at, :now) "
            "ELSE reported_at END "
            "WHERE id = :id",
            {
                "status": status,
                "new": STATUS_NEW,
                "reported": STATUS_REPORTED,
                "now": now_iso(),
                "id": vacancy_id,
            },
        )
        self._connection.commit()
        return cursor.rowcount > 0

    # --- 1: обогащение, единственная выборка, ходящая в сеть -------------

    # Предикат очереди обогащения. Один текст на две выборки — полную
    # (`pending_enrichment`) и дешёвую, из двух колонок (`pending_titles`):
    # они обязаны отбирать РОВНО одно множество, иначе префильтр судит не
    # о тех строках, которые пойдут в сеть.
    _PENDING_WHERE_SQL = (
        "WHERE status = ? AND description IS NULL "
        "AND CAST(COALESCE(enrich_attempts, 0) AS INTEGER) < ? "
        "ORDER BY COALESCE(published_at, first_seen_at) DESC"
    )

    def pending_titles(self, max_attempts: int) -> list[tuple[str, str]]:
        """`(id, заголовок)` всей очереди обогащения — всё, чем живёт отсев.

        Префильтр принимает решение по одному заголовку (`Prefilter.
        reason_for_title`), поэтому строить ради него `DiscoveredVacancy`
        из одиннадцати колонок незачем — а на бэклоге это была самая
        дорогая выборка шага, не ходящего в сеть вовсе.

        Лимита здесь нет СОЗНАТЕЛЬНО, и это не забывчивость. Отсев обязан
        накрывать очередь целиком: если бы он видел только первые
        `limit` строк, то правка `negative` доставала бы бэклог по кусочку,
        а главное — вакансии, вытесненные за границу окна отсева, но
        попавшие в окно обогащения после отказов, ушли бы в сеть, ни разу
        не пройдя единственный барьер перед ней. Цена ограничена формой
        строки: две короткие колонки вместо модели с зарплатой и датами.
        """
        rows = self._connection.execute(
            f"SELECT CAST(id AS BLOB) AS id, CAST(title AS BLOB) AS title FROM vacancy "
            f"{self._PENDING_WHERE_SQL}",
            (STATUS_NEW, max_attempts),
        ).fetchall()
        return safe_rows(rows, to_id_and_title, self._quarantine)

    def pending_enrichment(
        self, max_attempts: int, limit: int = DEFAULT_BATCH_LIMIT
    ) -> list[DiscoveredVacancy]:
        """Очередь обогащения — единственная выборка, ходящая в сеть.

        `limit` здесь не про память, а про нагрузку на источник: длина
        этой выборки И ЕСТЬ число запросов к hh.ru за прогон. Без него
        накопленный бэклог означал прогон длиной в часы (замер: 50
        листингов по 20 страниц — 20 990 запросов и 5.8 ч одних только
        пауз вежливости при `interval_hours: 4`, то есть демон работает
        встык, без пауз между прогонами). Порядок выборки — свежие
        первыми, поэтому потолок откладывает старое, а не теряет его.

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
            f"SELECT {_DISCOVERED_COLUMNS_SQL} FROM vacancy {self._PENDING_WHERE_SQL} LIMIT ?",
            (STATUS_NEW, max_attempts, limit),
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
        "salary_currency = COALESCE(:salary_currency, salary_currency), "
        "work_formats = COALESCE(:work_formats, work_formats)"
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
            # Отсортированный список через запятую — чтобы одно и то же
            # множество всегда давало один и тот же текст (иначе круговой
            # тест хранения и score_detail стали бы недетерминированными).
            # Пустое множество сериализуется в NULL, а не в "": колонка и
            # так уже различает «не обогащено» и «обогащено, форматов
            # нет» одинаково (§3 design — неизвестный формат штрафа не
            # даёт), а COALESCE выше не переписал бы существующее значение
            # пустой строкой, только NULL-ом.
            "work_formats": ",".join(sorted(details.work_formats)) or None,
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

    def stalled_by_attempts(self, max_attempts: int) -> int:
        """Сколько строк выведено из очереди обогащения снижением лимита попыток.

        Хранилище не может запретить правку конфига, но не имеет права
        дать её последствиям пройти незамеченными. `pending_enrichment`
        отбирает по `enrich_attempts < max_attempts`, поэтому уменьшение
        `enrich.max_attempts` делает уже потраченные попытки чрезмерными
        задним числом — строка остаётся `status='new'` с пустым описанием
        и становится невидимой ВСЕМ трём выборкам. Счётчик `stuck` её не
        видит (там `description IS NOT NULL`), и прогон после такой
        правки становился `ok`: статус улучшался оттого, что работа
        пропала.

        В штатном режиме результат всегда ноль: `bump_enrich_attempt`
        делает строку терминальной (`rejected`/`enrich_failed`) тем же
        оператором, которым доводит счётчик до лимита. Ненулевое значение
        означает ровно одно событие — лимит понизили.

        COUNT(*) по трём предикатам не декодирует ни одного значения,
        поэтому сторож не падает даже на полностью испорченной таблице.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS stalled FROM vacancy WHERE status = ? AND description IS NULL "
            "AND CAST(COALESCE(enrich_attempts, 0) AS INTEGER) >= ?",
            (STATUS_NEW, max_attempts),
        ).fetchone()
        return int(row["stalled"]) if row else 0

    def stalled_rows_hint(self, max_attempts: int) -> str:
        """Чем человек найдёт эти строки в ЭТОМ хранилище.

        Метод, а не строка в тексте лога конвейера, — и это не педантизм.
        Совет был написан SQL'ем прямо в `pipeline/enrichment.py`, то есть
        вне единственного слоя, знающего про SQL (§4.3): первая же правка
        схемы протухала бы молча, а `PostgresRepository` из §4.2 подсказал
        бы человеку запрос к чужой базе. Предикат здесь тот же, что у
        `stalled_by_attempts` строкой выше, — разойтись им нечем.
        """
        return (
            "SELECT id FROM vacancy WHERE status='new' AND description IS NULL "
            f"AND enrich_attempts >= {max_attempts}"
        )

    def corrupted_count(self) -> int:
        """Сколько строк ушло в терминальный карантин за жизнь этого объекта.

        Репозиторий живёт ровно один прогон, поэтому счётчик и есть
        «за прогон». Нужен конвейеру: карантин срабатывает ВНУТРИ выборок,
        и без этого числа потеря вакансии навсегда не отражалась ни
        статусом прогона, ни причиной, ни счётчиком.
        """
        return self._quarantine.terminated

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
        стоит до `max_attempts` запросов.

        Платится она за КАЖДЫЙ возврат, а не единожды за жизнь строки:
        счётчик обнуляется на каждом. Один возврат на одну правку
        конфига — вот точная формулировка, и её достаточно, чтобы
        бесконечного цикла не было. Возврат отбирает ровно те строки,
        которые текущий конфиг НЕ подтверждает, поэтому повторный вызов
        при неизменном конфиге не находит ничего; чтобы заплатить второй
        раз, стоп-слово нужно вернуть в `negative` и убрать оттуда снова.

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

    def pending_scoring(
        self, limit: int = DEFAULT_BATCH_LIMIT
    ) -> list[tuple[DiscoveredVacancy, VacancyDetails]]:
        """Описание есть, оценки нет: пересчитать локально.

        Ровно та щель, через которую вакансия раньше проваливалась мимо
        обеих выборок, — теперь это явное состояние со своей очередью, а
        не повод идти за уже скачанной страницей второй раз.

        Строки здесь несут описание, то есть по памяти стоят столько же,
        сколько `unreported()`; отсюда тот же `limit`. Считать застрявших
        по длине этой выборки поэтому нельзя — для счётчика `stuck` есть
        `count_pending_scoring()`, который не декодирует ни одного
        значения и не зависит от потолка.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(work_formats AS BLOB) AS work_formats "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL "
            "AND score_detail IS NULL ORDER BY COALESCE(published_at, first_seen_at) DESC "
            "LIMIT ?",
            (STATUS_NEW, limit),
        ).fetchall()
        return safe_rows(rows, to_scoring_task, self._quarantine)

    def count_pending_scoring(self) -> int:
        """Сколько вакансий ждут локального пересчёта. Тот же предикат.

        COUNT(*) вместо `len(pending_scoring())` по двум причинам сразу.
        Во-первых, счётчик `stuck` уезжает в журнал прогона и обязан
        оставаться верным при любом `limit`: иначе бэклог из десяти тысяч
        застрявших рапортовал бы ровно `limit`. Во-вторых, COUNT не
        декодирует ни одного значения, поэтому не падает даже на
        полностью испорченной таблице — тот же приём, что в
        `stalled_by_attempts`.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS pending FROM vacancy WHERE status = ? "
            "AND description IS NOT NULL AND score_detail IS NULL",
            (STATUS_NEW,),
        ).fetchone()
        return int(row["pending"]) if row else 0

    def save_score(self, vacancy_id: str, score: ScoreBreakdown) -> None:
        """Записать пересчитанную оценку, не трогая описание."""
        self._connection.execute(
            "UPDATE vacancy SET score = ?, score_detail = ? WHERE id = ?",
            (score.total, score.model_dump_json(), vacancy_id),
        )
        self._connection.commit()

    # --- 2.5: вектор описания ---------------------------------------------

    def pending_embedding(self, model: str, limit: int) -> list[tuple[str, str]]:
        """Описание есть, вектора текущей модели нет: отдать на эмбеддинг.

        Сравнение с `model` в предикате, а не проверка на NULL: правка
        `llm.embed_model` обязана ставить корпус в очередь заново сама.
        Иначе база разъехалась бы на две несравнимые половины, каждая из
        которых по отдельности выглядит здоровой.

        Отказ вакансии не помечается ничем, и счётчика попыток здесь нет
        сознательно. Отказ модели ничего не теряет: оценка по ключевым
        словам на месте, вакансия отправится и без вектора (§4 спеки
        2026-08-26), а следующий прогон попробует снова. Счётчик попыток
        нужен там, где повтор стоит запроса к чужому источнику, — здесь
        источник свой.
        """
        rows = self._connection.execute(
            "SELECT CAST(id AS BLOB) AS id, CAST(title AS BLOB) AS title, "
            "CAST(description AS BLOB) AS description FROM vacancy "
            "WHERE description IS NOT NULL AND description <> '' "
            "AND (embedding IS NULL OR embedding_model IS NOT ?) "
            "ORDER BY COALESCE(published_at, first_seen_at) DESC LIMIT ?",
            (model, limit),
        ).fetchall()
        return safe_rows(rows, to_embedding_task, self._quarantine)

    def save_embedding(self, vacancy_id: str, model: str, vector: bytes) -> None:
        """Записать вектор ВМЕСТЕ с именем модели — одним UPDATE.

        Двумя запросами между ними существовало бы состояние «вектор новой
        модели, имя старой», и падение процесса в этой щели оставило бы
        вектор, который выборки считают пригодным, а он из другого
        пространства.
        """
        self._connection.execute(
            "UPDATE vacancy SET embedding = ?, embedding_model = ? WHERE id = ?",
            (vector, model, vacancy_id),
        )
        self._connection.commit()

    def embeddings(self, ids: Sequence[str], model: str) -> dict[str, bytes]:
        """Векторы названных вакансий — только той модели, о которой спросили.

        Отдаёт СЫРЫЕ байты, а не разобранный вектор, и это граница слоя, а
        не лень. Формат упаковки принадлежит `llm/semantic.py`; знай о нём
        хранилище, оно перестало бы просто хранить BLOB и завело бы
        зависимость `storage → llm`, которой в этом проекте нет ни у
        одного слоя. Порчу разбирает тот, кто знает формат.
        """
        placeholders = ", ".join("?" * len(ids))
        rows = self._connection.execute(
            f"SELECT CAST(id AS BLOB) AS id, embedding FROM vacancy "  # noqa: S608 - плейсхолдеры
            f"WHERE id IN ({placeholders}) AND embedding IS NOT NULL AND embedding_model IS ?",
            (*ids, model),
        ).fetchall()
        vectors: dict[str, bytes] = {}
        for row in rows:
            try:
                vectors[decode_text(row["id"])] = bytes(row["embedding"])
            except UnicodeDecodeError:
                continue
        return vectors

    # --- 2.6: факты описания ----------------------------------------------

    def pending_facts(self, model: str, limit: int) -> list[tuple[str, str, str, float]]:
        """Описание есть, фактов ЭТОЙ модели нет: (id, заголовок, описание, оценка).

        Заголовок и описание отдаются порознь, а не склеенными, как в
        `pending_embedding`: эмбеддингу нужен один текст, а промпту —
        разные роли у этих двух полей.

        Ключевая оценка нужна конвейеру, чтобы решить, спрашивать ли у
        модели МНЕНИЕ: оно показывается только выше порога отчёта, и
        платить за него на всём корпусе незачем. Ноль у неоценённой
        строки — не «плохая вакансия», а «оценки ещё нет», и мнения у
        такой не спрашивают, что верно: спрашивать не о чем.
        """
        rows = self._connection.execute(
            "SELECT CAST(id AS BLOB) AS id, CAST(title AS BLOB) AS title, "
            "CAST(description AS BLOB) AS description, COALESCE(score, 0) AS score "
            "FROM vacancy WHERE description IS NOT NULL AND description <> '' "
            "AND (llm_facts IS NULL OR llm_facts_model IS NOT ?) "
            "ORDER BY COALESCE(published_at, first_seen_at) DESC LIMIT ?",
            (model, limit),
        ).fetchall()
        return safe_rows(rows, to_facts_task, self._quarantine)

    def save_facts(self, vacancy_id: str, model: str, facts: VacancyFacts) -> None:
        """Факты и имя извлёкшей их модели — одним UPDATE, не двумя."""
        self._connection.execute(
            "UPDATE vacancy SET llm_facts = ?, llm_facts_model = ? WHERE id = ?",
            (facts.model_dump_json(), model, vacancy_id),
        )
        self._connection.commit()

    def facts(self, ids: Sequence[str], model: str) -> dict[str, VacancyFacts]:
        """Факты названных вакансий — только той модели, о которой спросили.

        Нечитаемая запись пропускается вместе со своей вакансией и БЕЗ
        карантина, в отличие от `score_detail`. Разница по смыслу: без
        оценки вакансия не отправляется вовсе, поэтому её порча — авария,
        требующая лечения. Без фактов вакансия отправляется, просто без
        них, и заводить ради этого вторую очередь лечения значило бы
        платить сложностью за потерю, которой нет.
        """
        placeholders = ", ".join("?" * len(ids))
        rows = self._connection.execute(
            f"SELECT CAST(id AS BLOB) AS id, CAST(llm_facts AS BLOB) AS llm_facts "  # noqa: S608
            f"FROM vacancy WHERE id IN ({placeholders}) "
            "AND llm_facts IS NOT NULL AND llm_facts_model IS ?",
            (*ids, model),
        ).fetchall()
        extracted: dict[str, VacancyFacts] = {}
        for row in rows:
            try:
                extracted[decode_text(row["id"])] = VacancyFacts.model_validate_json(
                    decode_text(row["llm_facts"])
                )
            except (ValidationError, UnicodeDecodeError):
                continue
        return extracted

    # --- 3: отчёт --------------------------------------------------------

    def unreported(self, limit: int = DEFAULT_BATCH_LIMIT) -> list[ScoredVacancy]:
        """Готовое к отправке. Сторожа очереди пересчёта здесь БОЛЬШЕ НЕТ.

        Он тут был (`_warn_about_unscored`) и делал ровно две вещи, обе
        вредные. Во-первых, дублировал счётчик `stuck` из
        `pipeline/reporting.py`: тот считает то же самое через
        `pending_scoring()`, но добавляет id, понижает статус прогона и
        уезжает в журнал — то есть покрывает этот случай полностью и
        строго лучше. Во-вторых, `unreported()` вызывается конвейером
        дважды за прогон, поэтому один факт печатался тремя `ERROR`, и два
        из трёх ставили НЕВЕРНЫЙ диагноз: «конвейер не вызвал
        pending_scoring() перед unreported()». Трассировка SQL показывает
        обратное — за тот же прогон конвейер зовёт `pending_scoring()`
        трижды. Очередь не сходится не потому, что её не разобрали, а
        потому, что отказал скорер, и об этом строкой выше кричит сам
        `rescore`.

        Лог — единственный канал наблюдаемости у этого сервиса, и вся его
        диагностика построена на том, что сообщение называет причину.
        Сторож, называющий выдуманную причину, обесценивает остальные.

        `limit` обязателен по памяти, и это измерено: на 50 000 готовых
        строк выборка стоила 12.5 с и 762 МБ RSS — то есть OOM на VPS с
        гигабайтом. Копится очередь ровно в том сценарии, где отказ уже
        случился (приёмники не приняли отчёт), поэтому неограниченная
        выборка добавляла к отказу ещё и смерть процесса. Порядок
        `score DESC` делает потолок безобидным: отправляется самое высоко
        оценённое, остальное берёт следующий прогон.
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail, "
            "CAST(work_formats AS BLOB) AS work_formats "
            "FROM vacancy WHERE status = ? AND description IS NOT NULL "
            "AND score_detail IS NOT NULL ORDER BY score DESC LIMIT ?",
            (STATUS_NEW, limit),
        ).fetchall()
        return safe_rows(rows, to_scored, self._quarantine)

    def reported_since(
        self, cutoff: datetime, limit: int = DEFAULT_BATCH_LIMIT
    ) -> list[ScoredVacancy]:
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

        Потолок тот же и по той же причине: замер `report --since 60` на
        базе с 22 000 вакансий дал 351 МБ RSS, то есть OOM по команде
        человека на VPS с 512 МБ. Разница с `unreported()` в том, что
        здесь усечение видит человек, а не следующий прогон, — поэтому
        CLI обязан сказать вслух, что отчёт неполон (см. `__main__.py`).
        """
        rows = self._connection.execute(
            f"SELECT {_DISCOVERED_COLUMNS_SQL}, CAST(description AS BLOB) AS description, "
            "CAST(valid_through AS BLOB) AS valid_through, "
            "CAST(cluster AS BLOB) AS cluster, CAST(score_detail AS BLOB) AS score_detail, "
            "CAST(work_formats AS BLOB) AS work_formats "
            "FROM vacancy WHERE status = ? AND reported_at >= ? "
            "AND description IS NOT NULL AND score_detail IS NOT NULL "
            "ORDER BY score DESC LIMIT ?",
            (STATUS_REPORTED, to_utc_iso(cutoff), limit),
        ).fetchall()
        return safe_rows(rows, to_scored, self._quarantine)

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

    def close_abandoned_runs(self) -> int:
        return self._run_log.close_abandoned_runs()

    def last_successful_run(self) -> datetime | None:
        return self._run_log.last_successful_run()

    def last_run(self) -> RunSummary | None:
        return self._run_log.last_run()

    def cache_headers(self, url: str) -> dict[str, str]:
        return self._run_log.cache_headers(url)

    def save_cache_headers(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self._run_log.save_cache_headers(url, etag, last_modified)

    def reset_cache(self, url: str) -> None:
        self._run_log.reset_cache(url)

    # --- уборка: делегируется в Retention -------------------------------

    def descriptions_before(self, cutoff: datetime) -> tuple[int, int]:
        return self._retention.descriptions_before(cutoff)

    def forget_descriptions(self, cutoff: datetime) -> int:
        return self._retention.forget_descriptions(cutoff)

    def count_runs_before(self, cutoff: datetime) -> int:
        return self._retention.count_runs_before(cutoff)

    def forget_runs(self, cutoff: datetime) -> int:
        return self._retention.forget_runs(cutoff)

    def vacuum(self) -> None:
        self._retention.vacuum()
