import re
from collections.abc import Sequence
from datetime import datetime
from itertools import groupby
from pathlib import Path

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT

SNIPPET_LENGTH = 200

# Заголовок и описание пишет работодатель. `[Удалённо] Инженер` в начале
# названия на hh.ru встречается, а `**[Ссылка](https://evil/) конец]
# (https://hh.ru/vacancy/4)**` превращает пункт отчёта в рабочую ссылку на
# чужой сайт. Экранируется то, что меняет структуру строки. `<` и `>` —
# это две отдельные рабочие ссылки: автоссылка CommonMark
# `<https://evil.example/phish>` и сырой HTML `<a href=...>`, разрешённый
# markdown по умолчанию. Обе неотличимы в отчёте от нашей ссылки на hh.ru,
# а отчёт открывают именно затем, чтобы кликать.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")

# Управляющие символы, которые не несут текста, но доезжают до файла:
# нулевой байт и прочие C0/C1 ломают grep и часть редакторов, а
# двунаправленные управляющие (U+202A..U+202E, U+2066..U+2069 и метки
# U+200E/U+200F) переворачивают показ остатка строки — заголовок пишет
# работодатель, и отчёт читают глазами. Пробельные из диапазона не
# перечислены: их убирает `_collapse`.
_CONTROL = re.compile(
    r"[\x00-\x08\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]"
)

# Ссылки, уже вписанные в отчёт текущего дня. Экранированные скобки в счёт
# не идут: заголовок вида `Инженер](https://hh.ru/vacancy/2)` пишет
# работодатель, `_escape` превращает его в текст — и дедупликация обязана
# считать его текстом тоже, иначе чужой заголовок прячет настоящую вакансию
# из следующего отчёта. Слэши считаются ПАРАМИ: экранирован лишь тот `]`,
# перед которым их нечётное число. Одиночного `(?<!\\)` не хватает —
# заголовок, кончающийся на `\`, даёт `\\]` перед нашей же скобкой, и его
# ссылка перестала бы находиться (то есть вакансия попала бы в отчёт дважды).
_WRITTEN_LINK_RE = re.compile(r"(?<!\\)(?:\\\\)*\]\((https?://[^\s)]+)\)")


def _collapse(text: str) -> str:
    """Одна строка вместо любого числа: перевод строки внутри пункта списка
    ломает разметку не хуже скобки."""
    return " ".join(_CONTROL.sub("", text).split())


def _escape(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _plain(text: str | None, fallback: str = "—") -> str:
    return _escape(_collapse(text)) if text else fallback


class MarkdownSink:
    """Отчёт для чтения глазами: «Топ» по кластерам и свёрнутое «Остальное».

    Порог ничего не прячет, он меняет подробность показа (спека §6.3):
    вакансия на пороге РОВНО попадает в «Топ» (`>=`), а всё, что ниже, —
    одной строкой. Раздел «Остальное» и есть обратная связь по качеству
    скоринга, поэтому пустым он не остаётся молча.
    """

    name = "markdown"

    def __init__(self, reports_dir: Path, threshold: float) -> None:
        self._reports_dir = reports_dir
        self._threshold = threshold

    def emit(self, vacancies: Sequence[ScoredVacancy], now: datetime) -> None:
        if not vacancies:
            return
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        path = self._reports_dir / f"{now:%Y-%m-%d}-new.md"
        # Дедупликация по уже вписанным ссылкам: доставка сюда at-least-once
        # по построению (см. преамбулу задачи), и повтор снимается по факту
        # содержимого файла, а не по состоянию в памяти приёмника.
        existing = self._read_day_file(path)
        written = set(_WRITTEN_LINK_RE.findall(existing))
        ordered: list[ScoredVacancy] = []
        for item in sorted(vacancies, key=lambda item: item.score.total, reverse=True):
            if item.discovered.url in written:
                continue
            written.add(item.discovered.url)
            ordered.append(item)
        if not ordered:
            # Иначе к отчёту дописывался бы «# Новые вакансии» с пустыми
            # разделами — шум в файле, который читают глазами.
            return
        top = [item for item in ordered if item.score.total >= self._threshold]
        rest = [item for item in ordered if item.score.total < self._threshold]

        lines = [f"# Новые вакансии — {now:%Y-%m-%d %H:%M}", "", "## Топ", ""]
        if top:
            # Сортировка по кластеру устойчива, поэтому внутри кластера
            # сохраняется порядок по убыванию балла из `ordered`.
            for cluster, group in groupby(
                sorted(top, key=lambda item: item.cluster), key=lambda item: item.cluster
            ):
                lines += [f"### {cluster}", ""]
                lines += [self._full_entry(item) for item in group]
        else:
            lines += ["_ничего выше порога_", ""]

        lines += ["## Остальное", ""]
        lines += [self._short_entry(item) for item in rest] if rest else ["_пусто_", ""]

        # Дописывание, а не перезапись: прогон идёт раз в несколько часов, и
        # 'w' затирал бы утренние находки вечерними без следа — переотправки
        # нет по построению, `mark_reported` уводит вакансию из `unreported`
        # навсегда.
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                # Предыдущая запись оборвалась на полуслове (полный диск,
                # SIGKILL). Без этого перевода строки шапка нового отчёта
                # приклеивается к обрывку старого и перестаёт быть
                # заголовком — как и весь следующий за ней разбор.
                handle.write("\n")
            handle.write("\n".join(lines).rstrip() + "\n\n")

    def _read_day_file(self, path: Path) -> str:
        """Содержимое отчёта текущего дня; пусто, если файла нет.

        Чтение терпимое по той же причине, что и в CSV: запись, оборванная
        полным диском посреди кириллической буквы, оставляет в хвосте
        невалидный UTF-8 — и строгий декодер ронял бы КАЖДЫЙ следующий
        прогон до смены суток, вместо того чтобы дописать отчёт.
        """
        if not path.exists():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")

    def _full_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        published_at = discovered.published_at
        # За дату публикации заплачен запрос к странице вакансии, и в CSV
        # она есть: без неё свежую вакансию не отличить от
        # переопубликованной старой — а отчёт именно о новом. Дата
        # необязательна честно: листинг её не отдаёт (спека §5.3).
        published = (
            "дата неизвестна" if published_at is None else format(published_at, REPORT_DATE_FORMAT)
        )
        snippet = _escape(_collapse(item.details.description)[:SNIPPET_LENGTH])
        return (
            f"**[{_plain(discovered.title)}]({discovered.url})** — {item.score.total:.1f}\n\n"
            f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
            f"{_plain(discovered.salary.raw, fallback='зарплата не указана')} · "
            f"{published}\n\n"
            f"{snippet}…\n"
        )

    def _short_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        return (
            f"- [{_plain(discovered.title)}]({discovered.url}) — "
            f"{item.score.total:.1f} · {_plain(discovered.company)}"
        )
