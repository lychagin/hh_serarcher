"""Сборка текста `sendMessage`: шапка, «Топ» и честный хвост про файл.

Вынесено из `telegram_sink.py` отдельным модулем ради бюджета §4.3 основной
спеки: `emit()` приёмника уже занят дедупликацией по нескольким суткам,
черновиком и повторной доставкой документа (спека приёмника telegram §5,
находки 2026-07-30), и сборка текста сообщения стала там лишним весом.
Функции здесь чистые — ни сети, ни диска, — как и весь `html_report.py`.
"""

from collections.abc import Sequence

from hh_search.domain.models import ScoredVacancy
from hh_search.sinks.html_report import escape_attr, escape_html
from hh_search.sinks.telegram_client import MESSAGE_LIMIT, message_length


def render_message(fresh: Sequence[ScoredVacancy], threshold: float) -> str:
    """Шапка и «Топ» со ссылками, гарантированно короче `MESSAGE_LIMIT`.

    Счётчики здесь — отчёта, а не прогона: `Sink.emit` не получает
    `RunStats` и получать не должен, иначе ради одной строки текста
    пришлось бы менять интерфейс, общий с `csv` и `markdown` (спека §2).
    """
    top = sorted(
        (item for item in fresh if item.score.total >= threshold),
        key=lambda item: item.score.total,
        reverse=True,
    )
    head = f"<b>Новых вакансий: {len(fresh)}</b>, выше порога: {len(top)}"
    lines = [head]
    shown = 0
    for item in top:
        entry = _entry(item)
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
    if shown == 0 and top:
        # Ни одна запись не влезла целиком — она сама больше лимита вместе
        # с шапкой и хвостом. Обрезать готовую разметку нельзя (рвёт тег
        # или сущность, `400 can't parse entities`), поэтому запасной
        # вариант усекает ИСХОДНЫЙ заголовок до экранирования и до сборки
        # тега — молчать нельзя, хвост уже обещал «ещё N».
        minimal = _minimal_entry(top[0], head, len(top) - 1)
        if minimal is not None:
            lines.append(minimal)
            shown = 1
    if shown < len(top):
        lines.append(f"…ещё {len(top) - shown} — в файле")
    elif not top:
        lines.append("<i>ничего выше порога — подробности в файле</i>")
    return "\n\n".join(lines)


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
        f'<a href="{escape_attr(discovered.url)}">{escape_html(discovered.title)}</a> — '
        f"<b>{item.score.total:.1f}</b>\n{meta}"
    )


def _minimal_entry(item: ScoredVacancy, head: str, rest: int) -> str | None:
    """Запасной вариант: голая ссылка с усечённым заголовком, без метаданных.

    Заголовок усекается ИСХОДНЫМ текстом — двоичным поиском по длине
    префикса — а уже потом экранируется и оборачивается тегом. Обратный
    порядок (усечь готовую разметку) рвёт тег или именованную сущность
    (`&amp;` пополам) и даёт `400` у Bot API вместо честного «Топа».
    Двоичный поиск корректен, потому что `escape_html` не укорачивает
    текст: экранированная длина префикса растёт вместе с его длиной.
    Возвращает `None`, если не влезает даже пустой заголовок — тогда
    вызывающий код оставляет прежнее поведение (только честный хвост).
    """
    discovered = item.discovered
    prefix = f'<a href="{escape_attr(discovered.url)}">'
    suffix = f"</a> — <b>{item.score.total:.1f}</b>"
    tail = f"\n\n…ещё {rest} — в файле"

    def fits(length: int) -> bool:
        candidate = prefix + escape_html(discovered.title[:length]) + suffix
        return message_length("\n\n".join([head, candidate]) + tail) <= MESSAGE_LIMIT

    if not fits(0):
        return None
    low, high = 0, len(discovered.title)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return prefix + escape_html(discovered.title[:low]) + suffix
