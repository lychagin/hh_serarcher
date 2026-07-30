"""Формат работы со страницы вакансии — вынесен из `vacancy_page.py`.

Перенос по той же причине, что увёл сюда `salary.py`: `vacancy_page.py`
стоял вплотную к ориентиру §4.3 (≤150 строк кода), и добавление разбора
формата туда же перевело бы файл за границу без законной причины — не
переплетения инвариантов (как у `storage/repository.py` из таблицы
исключений), а просто ещё одного самостоятельного извлечения, как и
зарплата.

Источник — встроенное состояние страницы, а не блок `data-qa=
"work-formats-text"`: тот отдаёт локализованный текст («Формат работы:
удалённо»), который сменится от смены языка интерфейса, а перечисление
`workFormatsElement` — стабильный идентификатор hh.ru. Значения приходят
HTML-экранированными (`&#34;` вместо `"`), потому что весь блок вставлен
как значение HTML-атрибута, поэтому разбору предшествует `unescape`.
"""

import logging
import re
from dataclasses import dataclass
from html import unescape

from hh_search.domain.models import WorkFormat

logger = logging.getLogger(__name__)

# Ровно ключ `workFormatsElement`, а не более широкий `workFormats`:
# страница несёт под именем `workFormats` ещё два посторонних блока —
# краткую копию вида `"workFormats":["ON_SITE"]` в блоке похожих вакансий
# и СЛОВАРЬ всех значений перечисления для отрисовки фильтра
# (`"workFormats":[{"id":"ON_SITE","text":"..."},{"id":"REMOTE",...}]`).
# Второй — ловушка: в нём перечислены ВСЕ форматы разом, и совпадение по
# широкому ключу приписало бы любой вакансии полный набор форматов
# независимо от того, что у неё на самом деле указано. `workFormatsElement`
# встречается на живой странице ровно один раз (проверено на обеих
# фикстурах 2026-07-30) и относится именно к текущей вакансии.
_WORK_FORMATS_RE = re.compile(r'"workFormatsElement":\[([^\]]*)\]')
_TOKEN_RE = re.compile(r'"([A-Z_]+)"')


def extract_work_formats(html: str) -> frozenset[WorkFormat]:
    """Форматы работы вакансии. Пустое множество — блока нет или он пуст.

    Неизвестное значение перечисления (hh.ru заведёт новое — либо страница
    отдаст его раньше, чем мы узнаем про него) отбрасывается по одному, а
    не роняет разбор остальных: список — это `frozenset`, а не одно
    значение, и потеря соседних форматов из-за одного незнакомого была бы
    отдельной, никем не заказанной потерей.
    """
    match = _WORK_FORMATS_RE.search(unescape(html))
    if match is None:
        return frozenset()
    formats: set[WorkFormat] = set()
    for token in _TOKEN_RE.findall(match.group(1)):
        try:
            formats.add(WorkFormat(token))
        except ValueError:
            continue
    return frozenset(formats)


@dataclass
class WorkFormatBlockStats:
    """Счётчик страниц без формата работы — сторож дрейфа `workFormatsElement`.

    По образцу `SalaryBlockStats` из `vacancy_page.py`: по одной странице
    сказать нельзя ничего (формат мог быть не заполнен работодателем), а
    прогон, где он не нашёлся НИ РАЗУ, почти наверняка означает не рынок
    без форматов, а переименованный ключ встроенного состояния. Разница
    видна только в агрегате.
    """

    pages: int = 0
    without_formats: int = 0

    def record(self, formats: frozenset[WorkFormat]) -> None:
        self.pages += 1
        if not formats:
            self.without_formats += 1

    def log_summary(self) -> None:
        if not self.pages:
            return
        if self.without_formats == self.pages:
            logger.warning(
                "ни с одной из %d страниц вакансий не удалось прочитать формат работы "
                '(ключ "workFormatsElement" встроенного состояния). Либо ни у одной '
                "вакансии он не указан, либо ключ переименован (или состояние "
                "перестроено) и формат потерян для всех",
                self.pages,
            )
        elif self.without_formats:
            logger.info(
                "формат работы не указан у %d из %d страниц вакансий",
                self.without_formats,
                self.pages,
            )
