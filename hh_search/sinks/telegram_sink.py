"""Приёмник `telegram`: сборка отчёта поверх транспорта `telegram_client.py`.

Транспорт (учётные данные, `TelegramClient`, обработка отказов Bot API)
вынесен в отдельный модуль намеренно: этот класс зовёт только два публичных
метода `TelegramClient` и никогда не касается токена или URL — ровно так же,
как приёмники `csv`/`markdown` зовут чистые функции `html_report.py`, не
зная о транспорте вообще.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import (
    VACANCY_HREF_RE,
    document_header,
    escape_html,
    render_section,
)
from hh_search.sinks.telegram_client import MESSAGE_LIMIT, TelegramClient


class TelegramSink:
    """Отчёт в приватный канал: «Топ» сообщением, файл дня документом.

    Дедупликация — по файлу дня, тем же приёмом, что у `csv` и `markdown`:
    доставка сюда at-least-once по построению, потому что при отказе ЛЮБОГО
    приёмника `report()` не помечает вакансии отправленными и они приезжают
    снова.

    Порядок в `emit` — три шага, и запись файла стоит МЕЖДУ двумя отправками,
    а не до или после обеих (спека §5, раунд ревью 1):

    1. `send_message` — если падает, файл не тронут, следующий прогон
       повторит всё целиком; дубля нет, потому что сообщение не ушло.
    2. запись файла дня на диск.
    3. `send_document` — если падает, файл уже на месте: следующий прогон
       найдёт вакансии в файле дедупликацией, вернёт 0 и сообщение НЕ
       повторит.

    Файл до обеих отправок воспроизводил бы старую находку («сперва
    отправка, потом запись») — отказ `send_message` оставлял бы вакансии
    уже в файле, и следующий прогон не отправил бы их НИКОГДА. Файл после
    обеих — новую: `send_message` уходит успешно, `send_document` падает,
    файл остаётся пустым, и следующий прогон, не найдя вакансию в файле,
    шлёт то же сообщение в канал ВТОРОЙ раз. Место между отправками —
    единственное, где обе дыры закрыты одновременно: потеря на шаге 3
    ограничена документом ОДНОГО прогона и самоизлечивается — файл дня
    накопительный, и следующий вызов `send_document` в сутках довезёт его
    целиком, уже со всеми записями.
    """

    name = "telegram"

    def __init__(self, reports_dir: Path, threshold: float, client: TelegramClient) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold
        self._client = client

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        if not vacancies:
            return 0
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.html"
        existing = self._read_day_file(path)
        already = set(VACANCY_HREF_RE.findall(existing))
        fresh = [item for item in vacancies if item.discovered.url not in already]
        if not fresh:
            return 0

        section = render_section(fresh, now, self._threshold)
        document = (existing or document_header(now)) + section

        self._client.send_message(self._message(fresh))

        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")

        self._client.send_document(path.name, document.encode("utf-8"), f"Отчёт за {now:%Y-%m-%d}")
        return len(fresh)

    def _message(self, fresh: Sequence[ScoredVacancy]) -> str:
        """Шапка и «Топ» со ссылками, гарантированно короче `MESSAGE_LIMIT`.

        Счётчики здесь — отчёта, а не прогона: `Sink.emit` не получает
        `RunStats` и получать не должен, иначе ради одной строки текста
        пришлось бы менять интерфейс, общий с `csv` и `markdown` (спека §2).
        """
        top = sorted(
            (item for item in fresh if item.score.total >= self._threshold),
            key=lambda item: item.score.total,
            reverse=True,
        )
        head = f"<b>Новых вакансий: {len(fresh)}</b>, выше порога: {len(top)}"
        lines = [head]
        shown = 0
        for item in top:
            entry = self._entry(item)
            # Хвост объявляется честно, поэтому место под него резервируется
            # ДО того, как строка перестанет влезать.
            tail = f"\n\n…ещё {len(top) - shown} — в файле"
            if len("\n\n".join([*lines, entry])) + len(tail) > MESSAGE_LIMIT:
                break
            lines.append(entry)
            shown += 1
        if shown < len(top):
            lines.append(f"…ещё {len(top) - shown} — в файле")
        elif not top:
            lines.append("<i>ничего выше порога — подробности в файле</i>")
        return "\n\n".join(lines)

    @staticmethod
    def _entry(item: ScoredVacancy) -> str:
        discovered = item.discovered
        meta = " · ".join(
            part
            for part in (
                escape_html(discovered.company) if discovered.company else None,
                escape_html(discovered.area) if discovered.area else None,
                escape_html(discovered.salary.raw) if discovered.salary.raw else None,
            )
            if part
        )
        return (
            f'<a href="{discovered.url}">{escape_html(discovered.title)}</a> — '
            f"<b>{item.score.total:.1f}</b>\n{meta}"
        )

    def _read_day_file(self, path: Path) -> str:
        """Содержимое файла дня; пусто, если файла нет.

        Декодирование терпимое по той же причине, что в csv и markdown:
        запись, оборванная полным диском посреди кириллической буквы,
        оставляет в хвосте невалидный UTF-8, и строгий декодер ронял бы
        КАЖДЫЙ следующий прогон до смены суток.
        """
        if not path.exists():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")
