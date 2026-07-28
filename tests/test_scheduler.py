import os
import signal
import threading
import time
from pathlib import Path

import pytest

from hh_search.config.loader import load_config
from hh_search.config.models import Config
from hh_search.errors import AccessForbidden
from hh_search.pipeline.stats import FAILED, RunStats
from hh_search.scheduler import EXIT_FORBIDDEN, EXIT_OK, StopSignal, serve
from tests.test_config import write_config

HOUR = 3600.0


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """Настоящий валидированный конфиг, а не `model_construct`.

    `model_construct` проверок не выполняет, поэтому тест на нём проходил
    бы и с конфигом, который в проде не загрузится; заодно он не проходит
    `mypy --strict` — плагин pydantic требует все поля.
    """
    return load_config(write_config(tmp_path))


class FakeClock(StopSignal):
    """Часы и ожидание под контролем теста.

    Наследуется от `StopSignal`, а не подменяет `time.sleep`: расписание
    считается по монотонным часам, и подделать нужно именно их — иначе
    дрейф проверить нечем.
    """

    def __init__(self, run_duration: float = 0.0, stop_after: int | None = None) -> None:
        super().__init__()
        self.now = 1000.0
        self.delays: list[float] = []
        self.runs = 0
        self._run_duration = run_duration
        self._stop_after = stop_after

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds

    def run(self) -> None:
        self.runs += 1
        self.now += self._run_duration
        if self._stop_after is not None and self.runs >= self._stop_after:
            self.request()


def test_serve_makes_exactly_the_requested_number_of_runs(config: Config) -> None:
    """Три прогона — ДВЕ паузы: после последнего ждать незачем.

    Прежняя редакция закрепляла тестом лишний `sleep` после финального
    прогона, то есть фиксировала как правильное то, что просто удлиняло
    выход.
    """
    clock = FakeClock()
    assert serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=3) == 0
    assert clock.runs == 3
    assert clock.delays == [4 * HOUR, 4 * HOUR]


def test_schedule_does_not_drift_by_the_length_of_the_run(config: Config) -> None:
    """Пауза считается до ДЕДЛАЙНА, а не «интервал после прогона».

    Иначе к каждому интервалу прибавляется длительность прогона: при
    четырёх часах и десятиминутном прогоне сутки уносят расписание на два
    с половиной часа, а через неделю утренний прогон становится ночным.
    """
    clock = FakeClock(run_duration=600.0)
    serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=3)
    assert clock.delays == [4 * HOUR - 600.0, 4 * HOUR - 600.0]


def test_run_longer_than_the_interval_does_not_sleep_negative(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Прогон длиннее интервала: следующий начинается сразу, без sleep(-x)."""
    clock = FakeClock(run_duration=5 * HOUR)
    serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=2)
    assert clock.delays == []
    assert "дольше интервала" in caplog.text


def test_failing_run_does_not_stop_the_daemon(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Падение прогона не роняет демон: иначе контейнер уходит в петлю
    перезапусков, теряя расписание."""
    clock = FakeClock()
    attempts = {"n": 0}

    def failing() -> None:
        attempts["n"] += 1
        raise RuntimeError("сеть отвалилась")

    assert serve(config, failing, stop=clock, monotonic=clock.monotonic, iterations=2) == 0
    assert attempts["n"] == 2
    assert "продолжаем по расписанию" in caplog.text


# --- устойчивый 403: спека §9 требует остановки ----------------------------


def test_two_forbidden_in_a_row_stop_the_daemon(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Сервис, которому hh.ru закрыл доступ, обязан перестать стучаться.

    Прежняя редакция логировала 403 и продолжала цикл вечно: каждые четыре
    часа, годами, и никто об этом не узнавал. Спека §9 требует остановки с
    громким логом.
    """
    clock = FakeClock()
    calls = {"n": 0}

    def forbidden() -> None:
        calls["n"] += 1
        raise AccessForbidden("hh.ru ответил 403")

    code = serve(config, forbidden, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert code == EXIT_FORBIDDEN
    assert calls["n"] == 2
    assert "устойчиво" in caplog.text


def test_a_single_forbidden_between_successes_does_not_stop_the_daemon(config: Config) -> None:
    """Считаются ПОДРЯД идущие: одиночный 403 бывает антиботом на запросе."""
    clock = FakeClock()
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise AccessForbidden("hh.ru ответил 403")

    code = serve(config, flaky, stop=clock, monotonic=clock.monotonic, iterations=4)
    assert (code, calls["n"]) == (EXIT_OK, 4)


# --- остановка по сигналу --------------------------------------------------


def test_stop_request_ends_the_loop_without_another_run(config: Config) -> None:
    """Флаг проверяется МЕЖДУ прогонами: начатый прогон дорабатывает.

    `iterations=10`, но остановка запрошена во втором прогоне, значит
    третьего быть не должно — и ждать после него нечего.
    """
    clock = FakeClock(stop_after=2)
    code = serve(config, clock.run, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert (code, clock.runs, clock.delays) == (EXIT_OK, 2, [4 * HOUR])


def test_sigterm_sets_the_flag() -> None:
    """Обработчик SIGTERM обязан существовать.

    Ядро не применяет диспозицию по умолчанию к PID 1: без обработчика
    сигнал до процесса не доезжает, `docker stop` выжидает весь grace
    period и добивает SIGKILL (замер: 10.2 с и код 137 против 0.2 с и
    кода 0).
    """
    previous = signal.getsignal(signal.SIGTERM)
    stop = StopSignal()
    try:
        stop.install()
        assert not stop.requested()
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.requested()
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_waiting_is_interrupted_by_the_signal() -> None:
    """Обработчика мало: прерванный сигналом `time.sleep` возобновляется.

    PEP 475 повторяет прерванный вызов, поэтому обработчик, который только
    взводит флаг, оставил бы процесс спать все четыре часа — то есть
    SIGKILL всё равно. Здесь ожидание построено на `threading.Event`:
    `set()` из обработчика освобождает замок, и `wait()` возвращается
    немедленно. Если это сломать, тест не «упадёт быстро», а провисит
    указанные тридцать секунд — и именно это и есть проверяемый факт.
    """
    previous = signal.getsignal(signal.SIGTERM)
    stop = StopSignal()
    try:
        stop.install()
        timer = threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.monotonic()
        stop.wait(30.0)
        elapsed = time.monotonic() - started
        timer.cancel()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert stop.requested()
    assert elapsed < 5.0


def test_sigint_is_left_alone(config: Config) -> None:
    """Ctrl+C обязан прерывать процесс СРАЗУ, а не «после текущего прогона».

    Исключение SIGINT из `STOP_SIGNALS` объявлено докстрингом сознательным
    решением, но не сторожилось ничем: добавление сигнала в кортеж
    проходило весь набор тестов. Разница видна руками — при интерактивной
    отладке Ctrl+C переставал работать и приходилось ждать конца прогона
    или слать SIGKILL.
    """
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    stop = StopSignal()
    try:
        stop.install()
        assert signal.getsignal(signal.SIGINT) is before_int
        assert signal.getsignal(signal.SIGTERM) is not before_term
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.requested()
    finally:
        signal.signal(signal.SIGINT, before_int)
        signal.signal(signal.SIGTERM, before_term)


def test_keyboard_interrupt_ends_the_daemon_immediately(config: Config) -> None:
    """Оборотная сторона: KeyboardInterrupt выходит из цикла, а не гасится."""

    def interrupted() -> None:
        raise KeyboardInterrupt

    assert serve(config, interrupted, iterations=5) == EXIT_OK


# --- сценариев блокировки больше одного ------------------------------------
#
# `AccessForbidden` — не единственная форма отказа источника. Прогон, в
# котором hh.ru не отдал НИ ОДНОЙ страницы, уже помечается `failed`
# (`discovery._check_not_silent`), но демон на него не реагировал никак:
# счётчик считал только исключения, и, например, устойчивый 403 на
# `/robots.txt` (до этого раунда — `RobotsDisallowed`) крутил цикл вечно.


def blocked_stats() -> RunStats:
    """Прогон, в котором источник не отдал ни одной страницы."""
    stats = RunStats()
    stats.degrade(FAILED, "источник не отдал ни одной из 9 запрошенных страниц листингов")
    return stats


def test_two_runs_without_a_single_page_stop_the_daemon(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Два прогона подряд с нулём отданных страниц — тот же отказ доступа.

    Разница с `AccessForbidden` только в форме, в которой источник
    отказал; цена ошибки одна и та же — сервис стучится вечно и никто об
    этом не узнаёт.
    """
    clock = FakeClock()
    calls = {"n": 0}

    def blocked() -> RunStats:
        calls["n"] += 1
        return blocked_stats()

    code = serve(config, blocked, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert (code, calls["n"]) == (EXIT_FORBIDDEN, 2)
    assert "устойчиво" in caplog.text


def test_a_failed_run_that_still_got_pages_does_not_stop_the_daemon(config: Config) -> None:
    """`failed` бывает и по своей вине: том отчётов занят файлом.

    Останавливать демон навсегда из-за испорченного тома нельзя — это не
    отказ источника, и маркер потребовал бы человека там, где помогает
    следующий прогон.
    """
    clock = FakeClock()
    calls = {"n": 0}

    def failed_locally() -> RunStats:
        calls["n"] += 1
        stats = RunStats(discovered=18, enriched=1)
        stats.degrade(FAILED, "приёмники не приняли отчёт")
        return stats

    code = serve(config, failed_locally, stop=clock, monotonic=clock.monotonic, iterations=4)
    assert (code, calls["n"]) == (EXIT_OK, 4)


def test_a_single_empty_run_between_successes_does_not_stop_the_daemon(config: Config) -> None:
    """Считаются ПОДРЯД идущие, как и для 403: одна пустая выдача бывает."""
    clock = FakeClock()
    calls = {"n": 0}

    def flaky() -> RunStats:
        calls["n"] += 1
        return blocked_stats() if calls["n"] in (1, 3) else RunStats(discovered=18)

    code = serve(config, flaky, stop=clock, monotonic=clock.monotonic, iterations=4)
    assert (code, calls["n"]) == (EXIT_OK, 4)


def test_an_empty_run_after_a_forbidden_one_stops_the_daemon(config: Config) -> None:
    """Счётчик один на обе формы отказа: 403, затем пустой прогон — это два
    подряд идущих отказа источника, а не по одному в двух независимых."""
    clock = FakeClock()
    calls = {"n": 0}

    def blocked() -> RunStats:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AccessForbidden("hh.ru ответил 403")
        return blocked_stats()

    code = serve(config, blocked, stop=clock, monotonic=clock.monotonic, iterations=10)
    assert (code, calls["n"]) == (EXIT_FORBIDDEN, 2)
