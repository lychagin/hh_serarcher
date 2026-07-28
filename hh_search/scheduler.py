"""Цикл режима `serve`: расписание, остановка по сигналу, устойчивый 403."""

import logging
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

from hh_search.config.models import Config
from hh_search.errors import AccessForbidden

logger = logging.getLogger(__name__)

# Одиночный 403 бывает случайным (антибот на конкретном запросе). Второй
# ПОДРЯД — это уже устойчивый отказ доступа, а спека §9 требует на него
# остановку с громким логом. Стучаться каждые четыре часа вечно значит и
# продолжать нарушать запрет, и не дать никому об этом узнать.
MAX_FORBIDDEN_IN_A_ROW = 2

EXIT_OK = 0
EXIT_FORBIDDEN = 1

# Сигналы, по которым демон завершается штатно. SIGINT сюда не входит
# намеренно: Ctrl+C должен прерывать процесс сразу (KeyboardInterrupt),
# а не «после текущего прогона».
STOP_SIGNALS = (signal.SIGTERM,)


class StopSignal:
    """Флаг «пора остановиться» плюс прерываемое ожидание.

    Ожидание построено на `threading.Event`, а не на `time.sleep`, и это
    не стилистика. Ядро не применяет диспозицию по умолчанию к PID 1,
    поэтому без явного обработчика SIGTERM до процесса просто не
    доезжает: `docker stop` выжидает весь grace period и добивает
    SIGKILL (замер: 10.2 с и код 137 против 0.2 с и кода 0). А
    обработчика мало: по PEP 475 прерванный сигналом `time.sleep`
    возобновляется, то есть флаг взводится и процесс продолжает спать все
    четыре часа. `Event.set()` из обработчика освобождает замок, которого
    ждёт `Event.wait()`, поэтому ожидание кончается немедленно.

    Цена честного завершения — незакрытая строка `run` в журнале при
    остановке посреди прогона; конвейер закрывает её сам, потому что
    остановка проверяется только МЕЖДУ прогонами.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self, numbers: tuple[signal.Signals, ...] = STOP_SIGNALS) -> None:
        for number in numbers:
            signal.signal(number, self._handle)

    def _handle(self, number: int, frame: FrameType | None) -> None:
        logger.warning(
            "получен %s: завершаем работу после текущего прогона", signal.Signals(number).name
        )
        self.request()

    def request(self) -> None:
        """Попросить остановиться. Взводит флаг и обрывает ожидание."""
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        self._event.wait(seconds)


def serve(
    config: Config,
    run: Callable[[], object],
    *,
    stop: StopSignal | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    iterations: int | None = None,
) -> int:
    """Прогоны по расписанию. Возвращает код возврата процесса.

    Расписание считается по ДЕДЛАЙНУ, а не паузой после прогона: пауза на
    фиксированный интервал добавляет к нему длительность самого прогона, и
    при четырёх часах и десятиминутном прогоне сутки уносят расписание на
    два с половиной часа. Через неделю «утренний» прогон становится
    ночным.

    Отказ одного прогона не роняет демон (иначе контейнер уходил бы в
    петлю перезапусков), но `AccessForbidden` подряд — исключение из
    правила: см. `MAX_FORBIDDEN_IN_A_ROW`.
    """
    interval = config.app.schedule.interval_hours * 3600.0
    signal_ = stop if stop is not None else StopSignal()
    forbidden = 0
    completed = 0
    try:
        while iterations is None or completed < iterations:
            deadline = monotonic() + interval
            try:
                run()
            except AccessForbidden as error:
                forbidden += 1
                logger.error(
                    "hh.ru закрыл доступ (%d-й раз подряд): %s", forbidden, error, exc_info=True
                )
                if forbidden >= MAX_FORBIDDEN_IN_A_ROW:
                    logger.error(
                        "доступ закрыт устойчиво (%d прогона подряд), демон остановлен. "
                        "Обходные пути не применяются — нужен человек",
                        forbidden,
                    )
                    return EXIT_FORBIDDEN
            except Exception:
                logger.exception("прогон завершился с ошибкой, продолжаем по расписанию")
            else:
                forbidden = 0
            completed += 1
            if signal_.requested():
                break
            if iterations is not None and completed >= iterations:
                # Ждать после последнего прогона незачем: пауза перед
                # выходом только удлиняет тест и маскирует дрейф.
                break
            _wait_until(signal_, deadline - monotonic(), interval)
            if signal_.requested():
                break
    except KeyboardInterrupt:
        logger.warning("прерван с клавиатуры, выходим")
        return EXIT_OK
    logger.info("демон остановлен, выполнено прогонов: %d", completed)
    return EXIT_OK


def _wait_until(signal_: StopSignal, remaining: float, interval: float) -> None:
    if remaining <= 0:
        logger.warning(
            "прогон занял дольше интервала (%.0f с), следующий начинается немедленно; "
            "расписание не сдвигается",
            interval,
        )
        return
    signal_.wait(remaining)
