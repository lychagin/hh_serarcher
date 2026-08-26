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
from hh_search.sinks.ordering import by_relevance
from hh_search.sinks.text import format_work_formats, snippet

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
.snippet{font-size:13px}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}
a{color:#6ea8fe} h3,.meta{color:#aaa}}
"""


def escape_html(text: str) -> str:
    """Обезвредить три символа, которыми ломается СОДЕРЖИМОЕ элемента.

    Именно три, а не набор `MarkdownV2`: в HTML-режиме этого достаточно, и
    ровно поэтому формат сообщения выбран HTML (спека §2). Кавычки не
    экранируются здесь намеренно — этой функцией обрабатывается только
    содержимое элемента, где кавычка не опасна. Значение атрибута (`href`)
    экранируется отдельной `escape_attr`: у него кавычка обязана быть
    обезврежена, иначе она закрывает атрибут раньше времени.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(text: str) -> str:
    """Обезвредить значение HTML-атрибута в двойных кавычках (`href="..."`).

    Сегодня недостижимо: `url` собирается из id, прошедшего
    `^/vacancy/([1-9][0-9]{0,14})$`, то есть в нём нет ни кавычки, ни `&`.
    Зависеть от этого факта не стоит — источник `url` может измениться, а
    вёрстка не имеет права зависеть от чужой дисциплины. Строится поверх
    `escape_html`: тот уже вычищает `&`, `<`, `>`, остаётся обезвредить
    кавычку, которой в содержимом элемента не бывает.
    """
    return escape_html(text).replace('"', "&quot;")


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


def _entry(item: ScoredVacancy, *, excerpt: bool) -> str:
    """Пункт списка. `excerpt` — начало описания, как в markdown-отчёте.

    Выжимка только в «Топе»: там она стоит в `_full_entry` markdown-отчёта,
    а в «Остальном» — `_short_entry` одной строкой. Порог не прячет
    вакансию, он меняет подробность показа (спека §6.3 основной спеки), и
    два отчёта об одном и том же обязаны понимать это одинаково.
    """
    discovered = item.discovered
    published = (
        "дата неизвестна"
        if discovered.published_at is None
        else format(discovered.published_at, REPORT_DATE_FORMAT)
    )
    # Формат — своя константа из `_WORK_FORMAT_LABELS`, не текст работодателя,
    # и сегодня в подписях нет ни `<`, ни `>`, ни `&` — `escape_html` здесь
    # заведомый no-op, не защита от чего-то реального. Вызов оставлен ради
    # будущего: подпись — не текст работодателя сейчас, но станет уязвимым
    # местом, если словарь когда-нибудь пополнят строкой вроде
    # `"офис (R&D)"`, а исключение одного поля из общего пути экранирования
    # — источник расхождения при такой правке. Сторожит именно это —
    # `test_work_format_labels_contain_no_html_metacharacters` в
    # `tests/test_sinks.py`, а не сам вызов `escape_html`: он способен
    # покраснеть только на будущей подписи, не на сегодняшней.
    work_format = escape_html(format_work_formats(item.details.work_formats))
    meta = (
        f"{_plain(discovered.company)} · {_plain(discovered.area)} · "
        f"{_plain(discovered.salary.raw, fallback='зарплата не указана')} · {published} · "
        f"{work_format}"
    )
    description = (
        f'<br><span class="snippet">{escape_html(snippet(item.details.description))}…</span>'
        if excerpt
        else ""
    )
    return (
        f'<li><a href="{escape_attr(discovered.url)}">{escape_html(discovered.title)}</a> '
        f'<span class="score">{item.score.total:.1f}</span>'
        f'<br><span class="meta">{meta}</span>{description}</li>\n'
    )


def render_section(vacancies: Sequence[ScoredVacancy], now: datetime, threshold: float) -> str:
    """Блок одного прогона: «Топ» по кластерам и «Остальное» списком.

    Структура повторяет markdown-отчёт сознательно: два отчёта об одном и том
    же не должны расходиться в смысле — те же разделы, та же группировка по
    кластерам, то же начало описания в «Топе» и та же одна строка в
    «Остальном». Порог включающий (`>=`), как в §6.3 основной спеки.
    """
    ordered = by_relevance(vacancies)
    top = [item for item in ordered if item.score.total >= threshold]
    rest = [item for item in ordered if item.score.total < threshold]

    parts = [f"<section>\n<h2>Прогон {now:%H:%M} — Топ</h2>\n"]
    if top:
        for cluster, group in groupby(
            sorted(top, key=lambda item: item.cluster), key=lambda item: item.cluster
        ):
            parts.append(f"<h3>{escape_html(cluster)}</h3>\n<ul>\n")
            parts.extend(_entry(item, excerpt=True) for item in group)
            parts.append("</ul>\n")
    else:
        parts.append("<p><em>ничего выше порога</em></p>\n")

    parts.append("<h2>Остальное</h2>\n")
    if rest:
        parts.append("<ul>\n")
        parts.extend(_entry(item, excerpt=False) for item in rest)
        parts.append("</ul>\n")
    else:
        parts.append("<p><em>пусто</em></p>\n")
    parts.append("</section>\n")
    return "".join(parts)
