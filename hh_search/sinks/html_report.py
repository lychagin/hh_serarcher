"""Рендер HTML-отчёта: чистые функции, ни ввода-вывода, ни сети.

Формат выбран владельцем вместо markdown по фактической причине: телефон не
открывает `.md` браузером, и ссылки в нём остаются текстом. Кликабельность —
требование, а не удобство (спека §0).

Документ дописывается, а не перезаписывается: файл дня накапливает находки
нескольких прогонов. Перезапись означала бы разбор уже написанного HTML, а
разбор HTML запрещён решением §11 основной спеки. Поэтому шапка пишется один
раз, дальше каждый прогон дописывает `<section>`, а закрывающих тегов в файле
нет намеренно — браузер такой документ рендерит.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from itertools import groupby

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.base import REPORT_DATE_FORMAT

# Ссылки уже вписанных вакансий — вход дедупликации приёмника. Ограничение
# на `hh.ru/vacancy/` намеренное: под регулярку не должны попадать ссылки
# из шапки или подписи, иначе дедупликация начнёт считать отправленным то,
# чего в отчёте нет.
VACANCY_HREF_RE = re.compile(r'<a href="(https://hh\.ru/vacancy/[^"]+)"')

_STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;
line-height:1.5;background:#fff;color:#111}
h1{font-size:20px} h2{font-size:17px;margin-top:24px} h3{font-size:15px;color:#555}
a{color:#0a58ca;text-decoration:none} li{margin-bottom:10px}
.meta{color:#555;font-size:13px} .score{color:#0a7d28;font-weight:600}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}
a{color:#6ea8fe} h3,.meta{color:#aaa}}
"""


def escape_html(text: str) -> str:
    """Обезвредить три символа, которыми ломается разметка.

    Именно три, а не набор `MarkdownV2`: в HTML-режиме этого достаточно, и
    ровно поэтому формат сообщения выбран HTML (спека §2). Кавычки не
    экранируются — текст никогда не попадает в значение атрибута, только в
    содержимое элемента; ссылки строятся из `url`, который собран нами.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str | None, fallback: str = "—") -> str:
    return escape_html(text) if text else fallback


def document_header(now: datetime) -> str:
    """Шапка файла дня. Пишется один раз, при создании файла."""
    return (
        "<!doctype html>\n"
        '<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Вакансии {now:%Y-%m-%d}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>Вакансии — {now:%Y-%m-%d}</h1>\n"
    )


def _entry(item: ScoredVacancy) -> str:
    discovered = item.discovered
    published = (
        "дата неизвестна"
        if discovered.published_at is None
        else format(discovered.published_at, REPORT_DATE_FORMAT)
    )
    meta = (
        f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
        f"{_plain(discovered.salary.raw, fallback='зарплата не указана')} · {published}"
    )
    return (
        f'<li><a href="{discovered.url}">{escape_html(discovered.title)}</a> '
        f'<span class="score">{item.score.total:.1f}</span>'
        f'<br><span class="meta">{meta}</span></li>\n'
    )


def render_section(vacancies: Sequence[ScoredVacancy], now: datetime, threshold: float) -> str:
    """Блок одного прогона: «Топ» по кластерам и «Остальное» списком.

    Структура повторяет markdown-отчёт сознательно: два отчёта об одном и том
    же не должны расходиться в смысле. Порог включающий (`>=`), как в §6.3
    основной спеки.
    """
    ordered = sorted(vacancies, key=lambda item: item.score.total, reverse=True)
    top = [item for item in ordered if item.score.total >= threshold]
    rest = [item for item in ordered if item.score.total < threshold]

    parts = [f"<section>\n<h2>Прогон {now:%H:%M} — Топ</h2>\n"]
    if top:
        for cluster, group in groupby(
            sorted(top, key=lambda item: item.cluster), key=lambda item: item.cluster
        ):
            parts.append(f"<h3>{escape_html(cluster)}</h3>\n<ul>\n")
            parts.extend(_entry(item) for item in group)
            parts.append("</ul>\n")
    else:
        parts.append("<p><em>ничего выше порога</em></p>\n")

    parts.append("<h2>Остальное</h2>\n")
    if rest:
        parts.append("<ul>\n")
        parts.extend(_entry(item) for item in rest)
        parts.append("</ul>\n")
    else:
        parts.append("<p><em>пусто</em></p>\n")
    parts.append("</section>\n")
    return "".join(parts)
