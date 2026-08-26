"""Маппинг `sqlite3.Row` в доменные модели — по одному на выборку.

Текстовые колонки приходят как `bytes` (запрос делает `CAST(... AS
BLOB)`, см. repository.py) — decode здесь, а не в SQLite, чтобы битые
байты не роняли fetch всей строки, а превращались в обычный
`UnicodeDecodeError`, который `safe_rows` ловит и разбирает по месту.

Каждая функция здесь — «построитель» для `safe_rows`: она либо строит
модель, либо бросает исключение из `CORRUPTION_EXCEPTIONS`. Порядок
операций в `to_scored` значим: сначала то, что невосстановимо
(discovery-поля, описание), потом оценка — иначе строку с двумя видами
порчи сразу лечили бы локальным пересчётом, который её не спасёт.
"""

import json
import sqlite3
from datetime import datetime

from hh_search.domain.models import (
    DiscoveredVacancy,
    Salary,
    ScoreBreakdown,
    ScoredVacancy,
    VacancyDetails,
    WorkFormat,
)
from hh_search.storage.quarantine import CORRUPTION_EXCEPTIONS, ScoreUnreadable
from hh_search.storage.time_utils import parse_utc


def decode_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def decode_optional_text(value: bytes | str | None) -> str | None:
    return None if value is None else decode_text(value)


def decode_optional_int(value: bytes | str | None) -> int | None:
    """Числовые колонки тоже приходят как BLOB и тоже бывают испорчены.

    SQLite типизирован динамически: в колонке INTEGER может лежать текст,
    в том числе невалидный UTF-8, — и тогда fetch роняет ВЕСЬ курсор
    ровно так же, как на текстовой колонке. Поэтому CAST(... AS BLOB)
    накрывает и их, а разбор в число происходит здесь, где ValueError
    ловится по одной строке.
    """
    return None if value is None else int(decode_text(value))


def decode_optional_utc(value: bytes | str | None) -> datetime | None:
    """NULL в колонке даты — «ещё не обогащено», а не порча.

    До переезда discovery на листинг `published_at` заполнялся уже при
    вставке строки, из RSS. Теперь листинг даты не отдаёт, и NULL —
    штатное состояние вакансии между discovery и обогащением. Битую же
    строку по-прежнему разбирает `parse_utc`: её `ValueError` доезжает до
    `safe_rows` и отправляет запись в карантин.
    """
    text = decode_optional_text(value)
    return None if text is None else parse_utc(text)


def to_discovered(row: sqlite3.Row) -> DiscoveredVacancy:
    return DiscoveredVacancy(
        id=decode_text(row["id"]),
        url=decode_text(row["url"]),
        title=decode_text(row["title"]),
        company=decode_optional_text(row["company"]),
        area=decode_optional_text(row["area"]),
        salary=Salary(
            raw=decode_optional_text(row["salary_raw"]),
            amount_from=decode_optional_int(row["salary_from"]),
            amount_to=decode_optional_int(row["salary_to"]),
            currency=decode_optional_text(row["salary_currency"]),
        ),
        published_at=decode_optional_utc(row["published_at"]),
        found_by_query=decode_text(row["primary_query"]),
    )


def to_id_and_title(row: sqlite3.Row) -> tuple[str, str]:
    """Всё, чем располагает решение префильтра, — и ничего сверх того.

    Переоценка отбракованного отказа читает ровно две колонки: по
    остальным решение не принимается, а их порча не имеет права трогать
    вакансию, судьба которой определяется одним заголовком. Нечитаемый
    заголовок при этом решить ничего не позволяет, и `safe_rows` уводит
    такую строку в карантин — молча пропустить её значило бы оставить
    отказ необратимым и об этом промолчать.
    """
    return decode_text(row["id"]), decode_text(row["title"])


def decode_work_formats(value: bytes | str | None) -> frozenset[WorkFormat]:
    """CSV колонки → множество. NULL и незнакомое значение — не порча.

    NULL — так лежат вакансии, обогащённые до появления этой колонки, и
    страницы без блока формата; отличать эти случаи друг от друга смысла
    нет (§3 design: неизвестный формат штрафа не даёт). Незнакомый токен
    (hh.ru завёл значение раньше, чем мы о нём узнали) отбрасывается сам
    по себе, не роняя строку целиком — тот же приём, что в
    `extract_work_formats`.
    """
    text = decode_optional_text(value)
    if not text:
        return frozenset()
    formats: set[WorkFormat] = set()
    for token in text.split(","):
        try:
            formats.add(WorkFormat(token))
        except ValueError:
            continue
    return frozenset(formats)


def to_details(row: sqlite3.Row) -> VacancyDetails:
    """Поля страницы вакансии, прочитанные обратно — ВСЕ до единого.

    Компания, регион, зарплата и дата публикации лежат в тех же колонках
    `vacancy`, что читает `to_discovered`, и раньше здесь не читались
    вовсе: `VacancyDetails` записывался с ними, а возвращался без них.
    Асимметрия тиха и дорога — тип обещает поле, чтение молча отдаёт
    `None`, и приёмник отчёта, взявший `details.company`, получил бы
    пустую колонку без единого предупреждения. Двух представлений одной
    величины при этом не возникает: колонка одна, читатели просто разные —
    `DiscoveredVacancy` отвечает на «что мы знаем о вакансии», а
    `VacancyDetails` — на «что принесла страница», и именно в этом виде
    его принимает `save_enriched`.
    """
    return VacancyDetails(
        description=decode_text(row["description"]),
        valid_through=decode_optional_utc(row["valid_through"]),
        published_at=decode_optional_utc(row["published_at"]),
        company=decode_optional_text(row["company"]),
        area=decode_optional_text(row["area"]),
        salary=Salary(
            raw=decode_optional_text(row["salary_raw"]),
            amount_from=decode_optional_int(row["salary_from"]),
            amount_to=decode_optional_int(row["salary_to"]),
            currency=decode_optional_text(row["salary_currency"]),
        ),
        work_formats=decode_work_formats(row["work_formats"]),
    )


def to_embedding_task(row: sqlite3.Row) -> tuple[str, str]:
    """id и текст под эмбеддинг: заголовок плюс описание.

    Заголовок склеивается с описанием, а не эмбеддится отдельно, потому
    что решение принимается по вакансии целиком: «Team Lead» в заголовке
    и «Team Lead» в требованиях — разный вес для человека, но для
    косинуса к профилю важно, что и то и другое в этой вакансии есть.
    """
    return decode_text(row["id"]), f"{decode_text(row['title'])}\n{decode_text(row['description'])}"


def to_scoring_task(row: sqlite3.Row) -> tuple[DiscoveredVacancy, VacancyDetails]:
    """Описание уже скачано, оценки нет — всё для локального пересчёта."""
    return to_discovered(row), to_details(row)


def to_scored(row: sqlite3.Row) -> ScoredVacancy:
    discovered = to_discovered(row)
    details = to_details(row)
    cluster = decode_optional_text(row["cluster"]) or ""
    raw_score_detail = row["score_detail"]
    try:
        score = ScoreBreakdown.model_validate(json.loads(decode_text(raw_score_detail)))
    except CORRUPTION_EXCEPTIONS as corruption:
        raise ScoreUnreadable(raw_score_detail) from corruption
    return ScoredVacancy(discovered=discovered, details=details, score=score, cluster=cluster)
