"""Приёмник `telegram`: сборка отчёта поверх транспорта `telegram_client.py`.

Транспорт (учётные данные, `TelegramClient`, обработка отказов Bot API)
вынесен в отдельный модуль намеренно: этот класс зовёт только два публичных
метода `TelegramClient` и никогда не касается токена или URL — ровно так же,
как приёмники `csv`/`markdown` зовут чистые функции `html_report.py`, не
зная о транспорте вообще.
"""

import os
import tempfile
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import (
    VACANCY_HREF_RE,
    document_header,
    escape_html,
    render_section,
)
from hh_search.sinks.telegram_client import MESSAGE_LIMIT, TelegramClient, message_length


class TelegramSink:
    """Отчёт в приватный канал: «Топ» сообщением, файл дня документом.

    Дедупликация — по файлу дня, тем же приёмом, что у `csv` и `markdown`:
    доставка сюда at-least-once по построению, потому что при отказе ЛЮБОГО
    приёмника `report()` не помечает вакансии отправленными и они приезжают
    снова.

    Публикация файла дня АТОМАРНА и стоит МЕЖДУ двумя отправками (спека §5,
    редакция 4). Четыре шага:

    1. черновик пишется во временный файл рядом с файлом дня — то есть
       права и место на диске проверяются ДО всякой сети;
    2. `send_message` — если падает, черновик убирается, файл дня не
       тронут, следующий прогон повторит всё целиком; дубля нет, потому
       что сообщение не ушло;
    3. `os.replace` — публикация одним шагом, рваного файла дня не бывает;
    4. `send_document` — если падает, файл уже опубликован: следующий
       прогон найдёт вакансии в файле дедупликацией, вернёт 0 и сообщение
       НЕ повторит.

    Порядок «сообщение, потом запись» (редакция 3) воспроизводил критическую
    находку: запись сама может упасть. Каталог отчётов `chmod 0o500` — и
    сообщение уже ушло, а файла нет, поэтому следующий прогон видит
    вакансии новыми и шлёт то же сообщение снова, раз в четыре часа, пока
    том не починят. Черновик до сети закрывает это: отказ каталога больше
    не оставляет отправленного сообщения.

    Запись файла ДО обеих отправок воспроизводила бы первую находку —
    отказ `send_message` оставлял бы вакансии уже в файле, и следующий
    прогон не отправил бы их НИКОГДА. Запись ПОСЛЕ обеих — вторую:
    `send_message` уходит успешно, `send_document` падает, файла нет, и
    следующий прогон шлёт то же сообщение ВТОРОЙ раз. Публикация между
    отправками — единственное место, где закрыты обе: потеря на шаге 4
    ограничена документом ОДНОГО прогона и самоизлечивается, потому что
    файл дня накопительный.
    """

    name = "telegram"

    def __init__(self, reports_dir: Path, threshold: float, client: TelegramClient) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold
        self._client = client

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        if not vacancies:
            return 0
        path = self._day_file(now.date())
        existing = self._read_day_file(path)
        already = self._already(now, existing)
        fresh = [item for item in vacancies if item.discovered.url not in already]
        if not fresh:
            return 0

        document = (existing or document_header(now)) + render_section(fresh, now, self._threshold)
        payload = document.encode("utf-8")
        draft = self._draft(path, payload)
        try:
            self._client.send_message(self._message(fresh))
        except BaseException:
            # Черновик не имеет права остаться: каталог отчётов зарос бы
            # обрывками, а файл дня обязан дождаться повторного прогона
            # нетронутым.
            draft.unlink(missing_ok=True)
            raise
        os.replace(draft, path)

        self._client.send_document(path.name, payload, f"Отчёт за {now:%Y-%m-%d}")
        return len(fresh)

    def _day_file(self, day: date) -> Path:
        return self._reports_dir / f"{day:%Y-%m-%d}-new.html"

    def _draft(self, path: Path, payload: bytes) -> Path:
        """Черновик файла дня — в том же каталоге и ДО первой отправки.

        В том же каталоге, потому что `os.replace` атомарен только внутри
        одной файловой системы, а каталог отчётов — смонтированный том. До
        отправки — потому что иначе отказ записи (`:ro`, полный диск,
        сменившиеся права) случался бы уже ПОСЛЕ ушедшего сообщения.
        """
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self._reports_dir, prefix=path.name, suffix=".part")
        draft = Path(name)
        try:
            with os.fdopen(handle, "wb") as opened:
                opened.write(payload)
            # `mkstemp` даёт 0600, а файл дня обязан читаться так же, как его
            # соседи `-new.md` и `-new.csv`: README отправляет человека
            # открыть его браузером с хоста. Число, а не umask процесса:
            # 0644 — ровно то, что даёт обычная запись при штатном umask 022,
            # то есть режим соседей не меняется.
            draft.chmod(0o644)
        except BaseException:
            draft.unlink(missing_ok=True)
            raise
        return draft

    def _already(self, now: datetime, existing: str) -> set[str]:
        """Ссылки, уже уехавшие в канал, — за сегодня И за вчера.

        Вчерашний файл подмешивается потому, что ключ дедупликации — файл
        `<дата>-new.html`, то есть он новый каждые сутки, а `mark_reported`
        при отказе ЛЮБОГО приёмника не вызывался. Прогон 23:50 с упавшим
        `send_document` (или упавшим соседом-`csv`) оставлял вакансии
        непомеченными, и прогон 03:50 следующих суток, не найдя файла новых
        суток, дословно повторял вечернее сообщение. При `interval_hours: 4`
        это штатный исход любого вечернего отказа.

        Законно новые вакансии этим не глотаются, и это проверяется
        рассуждением, а не надеждой: вакансия, отправленная вчера и
        ПОМЕЧЕННАЯ, сегодня не придёт из `unreported()` вовсе; непомеченная
        же — ровно та, дубля которой мы избегаем. Вчерашний файл при этом
        только читается: документ дня собирается из `existing`, то есть из
        сегодняшнего.
        """
        yesterday = self._read_day_file(self._day_file(now.date() - timedelta(days=1)))
        return set(VACANCY_HREF_RE.findall(existing)) | set(VACANCY_HREF_RE.findall(yesterday))

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
            # Длина считается так, как её считает Telegram, — в кодовых
            # единицах UTF-16 (`message_length`). Счёт в кодовых точках
            # занижал её на каждом эмодзи в заголовке, и Bot API отвечал
            # 400 на сообщение, которое по нашему счёту влезало.
            if message_length("\n\n".join([*lines, entry]) + tail) > MESSAGE_LIMIT:
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

        Декодирование остаётся терпимым, хотя после перехода на атомарную
        публикацию (`os.replace` в `emit`) рваного файла дня эта версия
        кода уже не оставляет. Терпимость нужна для файла, рваного ПРЕЖНЕЙ
        версией: том переживает обновление образа, и первый же прогон после
        него читает то, что записала запись, оборванная полным диском
        посреди кириллической буквы. Строгий декодер ронял бы такой прогон
        и каждый следующий до смены суток — то есть чинить пришлось бы
        руками, а лечится это само.
        """
        if not path.exists():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")
