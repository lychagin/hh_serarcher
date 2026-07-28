import re
from collections.abc import Sequence
from datetime import datetime
from itertools import groupby
from pathlib import Path

from hh_search.domain.models import ScoredVacancy

SNIPPET_LENGTH = 200

# Заголовок и описание пишет работодатель. `[Удалённо] Инженер` в начале
# названия на hh.ru встречается, а `**[Ссылка](https://evil/) конец]
# (https://hh.ru/vacancy/4)**` превращает пункт отчёта в рабочую ссылку на
# чужой сайт. Экранируется то, что меняет структуру строки.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]])")

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
    return " ".join(text.split())


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
        written = self._written_urls(path)
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
            handle.write("\n".join(lines).rstrip() + "\n\n")

    def _written_urls(self, path: Path) -> set[str]:
        """Ссылки на вакансии, уже вписанные в отчёт текущего дня."""
        if not path.exists():
            return set()
        return set(_WRITTEN_LINK_RE.findall(path.read_text(encoding="utf-8")))

    def _full_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        snippet = _escape(_collapse(item.details.description)[:SNIPPET_LENGTH])
        return (
            f"**[{_plain(discovered.title)}]({discovered.url})** — {item.score.total:.0f}\n\n"
            f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
            f"{_plain(discovered.salary.raw, fallback='зарплата не указана')}\n\n"
            f"{snippet}…\n"
        )

    def _short_entry(self, item: ScoredVacancy) -> str:
        discovered = item.discovered
        return (
            f"- [{_plain(discovered.title)}]({discovered.url}) — "
            f"{item.score.total:.0f} · {_plain(discovered.company)}"
        )
