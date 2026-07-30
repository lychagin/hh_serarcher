"""Приёмник `telegram`: сборка отчёта поверх транспорта `telegram_client.py`.

Транспорт (учётные данные, `TelegramClient`, обработка отказов Bot API)
вынесен в отдельный модуль намеренно: этот класс зовёт только два публичных
метода `TelegramClient` и никогда не касается токена или URL — ровно так же,
как приёмники `csv`/`markdown` зовут чистые функции `html_report.py`, не
зная о транспорте вообще. Сборка текста сообщения — в `telegram_message.py`
(тот же принцип, применённый к бюджету §4.3: `emit()` здесь и так занят
дедупликацией по нескольким суткам, черновиком и повторной доставкой).
"""

import os
import tempfile
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import VACANCY_HREF_RE, document_header, render_section
from hh_search.sinks.telegram_client import TelegramClient
from hh_search.sinks.telegram_message import render_message

# Сколько СУТОК назад читать файлы отчёта ради дедупликации, помимо
# сегодняшнего (спека §5, item 3). Одних вчерашних было мало: отказ
# `send_document` вечером 29-го и повторный отказ ДОВОЗКИ (item 1) утром
# 30-го оставляют вакансию непомеченной ещё одни сутки, и прогон 31-го,
# заглянув только на день назад (в пустой файл 30-го — довозка ничего не
# пишет), не находит её нигде и повторяет вечернее сообщение дословно.
# Двух суток достаточно ровно для этого сценария: прогон 31-го видит файлы
# 30-го И 29-го.
#
# Шире — не значит безопаснее. `mark <id> new` возвращает вакансию в
# очередь, обнулив счётчик попыток, и если её ссылка совпадёт с файлом
# ПЯТИДНЕВНОЙ давности, широкое окно молча проглотит вручную возвращённую
# вакансию — находка ревью, которую сторожит
# `test_vacancy_outside_the_lookback_window_is_not_suppressed`. Окно
# намеренно узкое: ровно столько, сколько нужно для воспроизведённого
# сценария, и ни днём больше.
LOOKBACK_DAYS = 2

# Шаблон черновиков, которые эмиттер мог оставить сам: тот же суффикс, что
# и у `tempfile.mkstemp(..., suffix=".part")` в `_draft`, плюс общий для
# всех дней хвост имени файла дня. Уборка орфанов ищет РОВНО этот шаблон.
_DRAFT_GLOB = "*-new.html*.part"


class TelegramSink:
    """Отчёт в приватный канал: «Топ» сообщением, файл дня документом.

    Дедупликация — по файлам дня текущих и `LOOKBACK_DAYS` предыдущих
    суток, тем же приёмом, что у `csv` и `markdown`: доставка сюда
    at-least-once по построению, потому что при отказе ЛЮБОГО приёмника
    `report()` не помечает вакансии отправленными и они приезжают снова.

    Публикация файла дня АТОМАРНА и стоит МЕЖДУ двумя отправками (спека §5,
    редакция 4). Четыре шага:

    1. черновик пишется во временный файл рядом с файлом дня — то есть
       права и место на диске проверяются ДО всякой сети;
    2. `send_message` — если падает, черновик убирается, файл дня не
       тронут, следующий прогон повторит всё целиком; дубля нет, потому
       что сообщение не ушло;
    3. `os.replace` — публикация одним шагом, рваного файла дня не бывает;
       падение самого `os.replace` тоже убирает черновик и поднимает
       исключение (item 2) — каталог мог стать недоступен уже ПОСЛЕ
       `send_message`;
    4. `send_document` — если падает, файл уже опубликован: следующий
       прогон найдёт вакансии в файле дедупликацией и вернёт 0. Если при
       этом хоть одна пришедшая вакансия нашлась ТОЛЬКО в файле одних из
       предыдущих суток (а не сегодняшних), это и есть след той самой
       непровезённой отправки — `emit` довозит именно её документ, не
       трогая сообщение (item 1): оно уже ушло, повтор был бы дублем.

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
    ограничена документом ОДНОГО прогона и самоизлечивается через
    довозку (item 1), а не только «файл накопительный, подождём».
    """

    name = "telegram"

    def __init__(self, reports_dir: Path, threshold: float, client: TelegramClient) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold
        self._client = client

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> int:
        self._sweep_orphaned_drafts()
        if not vacancies:
            return 0
        path = self._day_file(now.date())
        existing = self._read_day_file(path)
        today_hrefs = set(VACANCY_HREF_RE.findall(existing))
        previous = self._previous_days(now.date())
        previous_hrefs: set[str] = set()
        for _, _, hrefs in previous:
            previous_hrefs |= hrefs

        already = set(today_hrefs)
        fresh: list[ScoredVacancy] = []
        for item in vacancies:
            url = item.discovered.url
            if url in already or url in previous_hrefs:
                continue
            # Пополняем на ходу: дубль может прийти и внутри одной пачки
            # (см. `CsvSink.emit` — тот же приём и та же причина).
            already.add(url)
            fresh.append(item)
        if not fresh:
            return self._redeliver(vacancies, today_hrefs, previous)

        document = (existing or document_header(now)) + render_section(fresh, now, self._threshold)
        payload = document.encode("utf-8")
        draft = self._draft(path, payload)
        try:
            self._client.send_message(render_message(fresh, self._threshold))
        except BaseException:
            # Черновик не имеет права остаться: каталог отчётов зарос бы
            # обрывками, а файл дня обязан дождаться повторного прогона
            # нетронутым.
            draft.unlink(missing_ok=True)
            raise
        try:
            os.replace(draft, path)
        except OSError:
            # Каталог мог стать недоступен уже ПОСЛЕ `send_message`
            # (item 2): черновик всё равно не имеет права остаться.
            draft.unlink(missing_ok=True)
            raise

        self._client.send_document(path.name, payload, f"Отчёт за {now:%Y-%m-%d}")
        return len(fresh)

    def _redeliver(
        self,
        vacancies: Sequence[ScoredVacancy],
        today_hrefs: set[str],
        previous: Sequence[tuple[date, str, set[str]]],
    ) -> int:
        """Довезти документ ОДНИХ предыдущих суток, если он застрял (item 1).

        Свежих вакансий нет — обычно это либо пустой прогон, либо повтор
        того же дня, и путь молчит, как раньше. Но если хоть одна ПРИШЕДШАЯ
        вакансия нашлась ТОЛЬКО в файле одних из `previous` суток (а не в
        сегодняшнем), значит тот прогон отправил сообщение и упал именно на
        `send_document` — и до сих пор не был повторён. Редоставка шлёт
        РОВНО тот файл, ничего не пишет и не трогает `send_message`: он уже
        ушёл, повтор был бы тем самым дублем, которого избегает дедуп.

        Если `send_document` падает снова — исключение поднимается как
        есть: следующий прогон обязан попробовать ещё раз, а не смолчать.
        """
        incoming = {item.discovered.url for item in vacancies}
        for day, content, hrefs in previous:
            stuck = (incoming & hrefs) - today_hrefs
            if content and stuck:
                self._client.send_document(
                    self._day_file(day).name, content.encode("utf-8"), f"Отчёт за {day:%Y-%m-%d}"
                )
                return 0
        return 0

    def _previous_days(self, today: date) -> list[tuple[date, str, set[str]]]:
        """Файлы `LOOKBACK_DAYS` предыдущих суток: (дата, содержимое, ссылки).

        Ближайший день — первым: `_redeliver` останавливается на первом
        совпадении, а застрять чаще всего может именно вчерашний файл.
        """
        result: list[tuple[date, str, set[str]]] = []
        for offset in range(1, LOOKBACK_DAYS + 1):
            day = today - timedelta(days=offset)
            content = self._read_day_file(self._day_file(day))
            result.append((day, content, set(VACANCY_HREF_RE.findall(content))))
        return result

    def _day_file(self, day: date) -> Path:
        return self._reports_dir / f"{day:%Y-%m-%d}-new.html"

    def _sweep_orphaned_drafts(self) -> None:
        """Убрать черновики, осиротевшие убийством процесса между
        `mkstemp` и `os.replace` предыдущего прогона (item 2).

        Безопасно ровно потому, что `emit` вызывается под общим замком
        прогона (`single_run`, `hh_search/__main__.py`): пока этот вызов
        идёт, другой процесс с тем же каталогом отчётов не работает, а
        значит черновик, уже лежащий здесь В НАЧАЛЕ вызова, не может
        принадлежать работающему соседу — только мёртвому. Свежий
        черновик ЭТОГО вызова создаётся позже, самим `_draft`, и сюда не
        попадает.
        """
        if not self._reports_dir.exists():
            return
        for draft in self._reports_dir.glob(_DRAFT_GLOB):
            draft.unlink(missing_ok=True)

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
