"""Однотипные отказы — одной строкой, а не стеной почти одинаковых.

Замер аварии источника на FIX_BASE: 16 `WARNING` по ~300 символов,
отличающихся только URL, и в каждом — полный текст про robots.txt. При
образцовом конфиге (две страницы листинга и накопленный бэклог) это
больше сотни строк за прогон. Здоровый прогон при этом пишет три строки
`INFO`, то есть баланс перекошен ровно наоборот: в норме почти ничего, в
аварии — стена, в которой тонет единственная строка, называющая причину.

Группировка идёт по ТЕКСТУ причины, а не по типу исключения: при отказе
источника текст один и тот же для всех URL (вердикт robots.txt кэшируется
по origin вместе с типом), а разные причины лечатся по-разному и
склеивать их нельзя. Адреса печатаются выборкой: полный список нужен
тому, кто чинит вакансии поимённо, и для таких мест у сводки есть
`limit=None` — например, вакансии, потерянные терминально.
"""

import logging

logger = logging.getLogger(__name__)

# Сколько адресов показывать в сводке. Три — чтобы строка оставалась
# читаемой и всё же отвечала на вопрос «а какие именно».
SAMPLE = 3
# Чем заменяется собственный адрес отказа внутри текста причины.
PLACEHOLDER = "<адрес>"


class FailureDigest:
    """Копилка однотипных отказов шага. Порядок причин — как они случились."""

    def __init__(self) -> None:
        self._by_reason: dict[str, list[str]] = {}
        self.count = 0

    def add(self, reason: str, target: str) -> None:
        """Запомнить отказ. Подробность по каждому — в DEBUG, не в WARNING.

        Собственный адрес вычёркивается из текста причины: половина
        сообщений его называет («не удалось получить <url>: Network is
        unreachable»), и без этого одинаковые по сути отказы двадцати
        страниц дали бы двадцать «разных» причин — то есть ровно ту
        стену, ради которой копилка и заведена.
        """
        self.count += 1
        key = reason.replace(target, PLACEHOLDER) if target in reason else reason
        self._by_reason.setdefault(key, []).append(target)
        logger.debug("%s: %s", target, reason)

    def log_summary(self, what: str, limit: int | None = SAMPLE) -> None:
        """По одной строке `WARNING` на каждую РАЗНУЮ причину."""
        for reason, targets in self._by_reason.items():
            if len(targets) == 1:
                logger.warning("%s: %s — %s", what, targets[0], _sentence(reason))
                continue
            logger.warning(
                "%s: %d, причина у всех одна — %s. Адреса: %s",
                what,
                len(targets),
                _sentence(reason),
                _listed(targets, limit),
            )


def _sentence(reason: str) -> str:
    """Причина без хвостовой точки: её ставит сама сводка.

    Тексты исключений в проекте бывают и с точкой, и без, а склейка обоих
    видов даёт «…по умолчанию.. Адреса: …» — ровно та мелочь, из-за
    которой сообщение перестают читать.
    """
    return reason.rstrip(" .")


def _listed(targets: list[str], limit: int | None) -> str:
    if limit is None or len(targets) <= limit:
        return ", ".join(targets)
    return f"{', '.join(targets[:limit])} и ещё {len(targets) - limit}"
