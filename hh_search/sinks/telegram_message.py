"""Сборка текста `sendMessage`: макет «Находка дня» (спека 2026-08-02).

Вынесено из `telegram_sink.py` отдельным модулем ради бюджета §4.3 основной
спеки: `emit()` приёмника уже занят дедупликацией по нескольким суткам,
черновиком и повторной доставкой документа. Функции здесь чистые — ни сети,
ни диска, — как и весь `html_report.py`.
"""

from collections.abc import Sequence
from datetime import datetime

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import escape_attr, escape_html
from hh_search.sinks.telegram_client import MESSAGE_LIMIT, message_length
from hh_search.sinks.text import format_day, format_published, format_salary_short

# Сколько вакансий выше порога уходит в сообщение: карточка плюс шесть
# строк. Не «сколько влезет в 4096»: смысл макета — «читается за три
# секунды», а выше порога в живом прогоне бывает и сорок штук. Остаток
# честно назван хвостом и лежит в файле дня.
TOP_LIMIT = 7
# Пороги значков. Абсолютные и взятые из макета: при `report_threshold: 60`
# шкала работает как задумана. Порог выше 80 сделал бы все значки
# одинаковыми — это видно с первого же сообщения и чинится здесь, поэтому в
# конфиг не выносится (§1 спеки вида сообщения).
TIER_HOT = 80.0
TIER_WARM = 70.0

_EMPTY_TOP = "<i>ничего выше порога — подробности в файле</i>"


def render_message(fresh: Sequence[ScoredVacancy], threshold: float, now: datetime) -> str:
    """Сообщение целиком, гарантированно короче `MESSAGE_LIMIT`.

    Счётчики здесь — отчёта, а не прогона: `Sink.emit` не получает
    `RunStats` и получать не должен, иначе ради одной строки текста
    пришлось бы менять интерфейс, общий с `csv` и `markdown` (спека §2).

    Длина подбирается перебором СВЕРХУ ВНИЗ: собирается сообщение на
    `TOP_LIMIT` записей, затем на одну меньше, и так до первого влезающего.
    Перебор, а не наращивание с резервом места под хвост, потому что и
    хвост, и подзаголовок «ЕЩЁ N» зависят от числа показанных записей —
    наращивание считало бы их до того, как оно известно. Семь сборок дешевле
    одной сетевой ошибки.
    """
    top = sorted(
        (item for item in fresh if item.score.total >= threshold),
        key=lambda item: item.score.total,
        reverse=True,
    )
    below = len(fresh) - len(top)
    head = _head(now, len(fresh), len(top))
    if not top:
        return f"{head}\n\n{_EMPTY_TOP}"
    limited = top[:TOP_LIMIT]
    for count in range(len(limited), 0, -1):
        message = _assemble(limited[:count], now, head, len(top), below)
        if message_length(message) <= MESSAGE_LIMIT:
            return message
    return _minimal_message(top[0], head, len(top), below)


def _head(now: datetime, total: int, above: int) -> str:
    return f"{format_day(now)} · новых <b>{total}</b>, выше порога <b>{above}</b>"


def _assemble(
    items: Sequence[ScoredVacancy], now: datetime, head: str, above: int, below: int
) -> str:
    """Сообщение из заданного числа записей — без проверки длины."""
    return "\n\n".join([head, _card(items[0], now)])


def _card(item: ScoredVacancy, now: datetime) -> str:
    """Карточка лучшего совпадения: подзаголовок и цитата.

    Значка тира здесь нет намеренно: его место занимает `★`, а первой
    записи значок «она лучшая» ничего не добавляет.
    """
    discovered = item.discovered
    link = f'<a href="{escape_attr(discovered.url)}">{escape_html(discovered.title)}</a>'
    meta = _meta(item, published=format_published(discovered.published_at, now))
    body = f"<b>{link}</b>\n{meta}" if meta else f"<b>{link}</b>"
    subheader = f"<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ · {item.score.total:.1f}</code>"
    return f"{subheader}\n<blockquote>{body}</blockquote>"


def _meta(item: ScoredVacancy, published: str | None = None) -> str:
    """«компания · регион · зарплата [· дата]»; пустые части не оставляют
    после себя разделителя.

    Экранируется всё, включая зарплату: токен валюты приходит из разметки
    hh.ru (`_CURRENCY_TOKEN_RE` пропускает и `<`), то есть это чужой текст,
    а не наше форматирование.
    """
    discovered = item.discovered
    salary = format_salary_short(discovered.salary)
    parts = (
        escape_html(discovered.company) if discovered.company else None,
        escape_html(discovered.area) if discovered.area else None,
        escape_html(salary) if salary else None,
        published,
    )
    return " · ".join(part for part in parts if part)


def _minimal_message(item: ScoredVacancy, head: str, above: int, below: int) -> str:
    """Запасной вариант: карточка с усечённым заголовком, без мета-строки.

    Заголовок усекается ИСХОДНЫМ текстом — двоичным поиском по длине
    префикса — а уже потом экранируется и оборачивается тегом. Обратный
    порядок (усечь готовую разметку) рвёт тег или именованную сущность
    (`&amp;` пополам) и даёт `400 can't parse entities` у Bot API вместо
    честного сообщения. Двоичный поиск корректен, потому что `escape_html`
    не укорачивает текст: экранированная длина префикса растёт вместе с
    длиной префикса.
    """
    prefix = (
        f"<code>★ ЛУЧШЕЕ СОВПАДЕНИЕ · {item.score.total:.1f}</code>\n"
        f'<blockquote><b><a href="{escape_attr(item.discovered.url)}">'
    )
    suffix = "</a></b></blockquote>"
    title = item.discovered.title

    def fits(length: int) -> bool:
        card = prefix + escape_html(title[:length]) + suffix
        return message_length(f"{head}\n\n{card}") <= MESSAGE_LIMIT

    if not fits(0):
        # Не влезает даже пустой заголовок — показывать нечего, но молчать
        # нельзя: файл дня уже уехал, и человек обязан узнать, что в нём.
        return head
    low, high = 0, len(title)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return f"{head}\n\n{prefix}{escape_html(title[:low])}{suffix}"
