"""Датаклассы уборки: сроки хранения и план/результат (спека 2026-08-01 §3).

Вынесено из `cleanup.py` (ревью Task 5, раунд починки 1): Important-1
(отказ каждого шага базы в `execute()` ловится отдельно, вместо того
чтобы улетать исключением мимо `CleanupPlan.errors`) и Minor-3/Minor-4
(поля `reports_considered`/`vacuum_ok`, сообщение об отрицательном сроке
по имени флага, а не поля) добавили оркестровке достаточно строк, чтобы
файл перешагнул бюджет 150 строк кода — тот же ориентир, по которому
раньше выделялся `report_files.py`. Файл делится, а не получает
строку-исключение в §4.3 спеки: решение владельца, принятое трижды
(`CLAUDE.md`). Здесь — только форма данных и человекочитаемый текст;
порядок вызовов и SQL остаются в `cleanup.py` и `storage/retention.py`.
"""

from dataclasses import dataclass
from datetime import datetime

__all__ = ["CleanupDays", "CleanupPlan"]

_MB = 1024 * 1024


@dataclass(frozen=True)
class CleanupDays:
    """Сроки хранения. `reports=None` означает «файлы не трогать вовсе».

    Одним полем выражены и флаг `--reports`, и его срок: два поля
    разъехались бы при первой же правке CLI.
    """

    descriptions: int = 90
    runs: int = 365
    reports: int | None = None

    # Имя, которое человек НАБИРАЛ во флаге CLI, а не имя поля датакласса
    # (ревью Task 5, раунд починки 1, Minor-4): «срок хранения descriptions
    # не может быть отрицательным» не отвечает на вопрос, какой флаг чинить.
    # У `reports` соответствие флагу неоднозначно само по себе — одно поле
    # держит и срок, и включённость `--reports` (см. докстринг класса), —
    # поэтому в подсказке назван и срок, и его условие.
    _FLAG_NAMES = {
        "descriptions": "--descriptions-days",
        "runs": "--runs-days",
        "reports": "--reports-days (учитывается только вместе с --reports)",
    }

    def __post_init__(self) -> None:
        """Отрицательный срок отвергается, ноль — разрешён.

        Отрицательный срок задаёт границу В БУДУЩЕМ (`now -
        timedelta(days=-N)`), и уборка обнулила бы описание вакансии,
        отправленной секунду назад, — между `--descriptions-days -365` и
        «стереть всё» не стояло бы ничего. Ноль не отвергается: это
        предельно короткий, но осмысленный срок (`CleanupDays(reports=0)`
        — законное ручное действие, проверяемое сторожем окна довозки).
        Проверка живёт здесь, а не в CLI, — так её получит любой
        вызывающий, а не только команда.
        """
        for name, value in (
            ("descriptions", self.descriptions),
            ("runs", self.runs),
            ("reports", self.reports),
        ):
            if value is not None and value < 0:
                raise ValueError(
                    f"срок хранения {self._FLAG_NAMES[name]} не может быть отрицательным: {value}"
                )


@dataclass(frozen=True)
class CleanupPlan:
    """Что уборка сделает или сделала. Одна форма на оба случая.

    Одна, а не две: сухой прогон обязан печатать РОВНО то, что напечатал
    бы `--apply`, иначе он перестаёт быть предпросмотром.
    """

    descriptions: int
    description_bytes: int
    runs: int
    report_files: int
    report_bytes: int
    descriptions_cutoff: datetime
    # `False`, когда `--reports` не передан (ревью Task 5, раунд починки 1,
    # Minor-3): без него `report_files`/`report_bytes` всегда нулевые, а
    # текст «файлов отчётов: 0 — убрано» читался так, будто файлы проверили
    # и не нашли ни одного, — человек решал, что уборка их уже почистила.
    # Поле различает «проверили — нашли 0» от «не смотрели вовсе».
    reports_considered: bool = True
    # `False`, если `VACUUM` отказал (ревью Task 5, раунд починки 1,
    # Important-1, тот же принцип: тексты не имеют права обещать того, чего
    # не было). Без этого поля `describe()` при отказавшем `VACUUM` печатал
    # бы «база ужата» рядом со строкой «ОШИБКА: база не ужата» — два
    # противоречащих друг другу утверждения в одном выводе.
    vacuum_ok: bool = True
    # Причины, по которым часть уборки не удалась (недоступный каталог
    # отчётов, гонка при удалении файла, отказ записи горизонта). Пустой
    # кортеж — уборка прошла без сучка. Поле обязано жить В ВОЗВРАЩЁННОМ
    # значении, а не только в логе (ревью Task 4, общее требование раунда
    # 1): человек не имеет права остаться в неведении, случилась ли
    # уборка целиком; текст переиспользует CLI будущей задачи.
    errors: tuple[str, ...] = ()

    def describe(self, applied: bool) -> str:
        verb = "убрано" if applied else "будет убрано"
        lines = [
            f"описаний: {self.descriptions} ({self.description_bytes / _MB:.1f} МБ) — {verb}",
            f"строк журнала прогонов: {self.runs} — {verb}",
        ]
        if self.reports_considered:
            lines.append(
                f"файлов отчётов: {self.report_files} ({self.report_bytes / _MB:.1f} МБ) — {verb}"
            )
        else:
            lines.append("файлы отчётов: не рассматривались — флаг `--reports` не передан")
        lines.append(f"граница хранения описаний: {self.descriptions_cutoff:%Y-%m-%d}")
        if applied and self.vacuum_ok:
            lines.append(
                "база ужата (VACUUM). `report --since` за границу описаний "
                "больше не покажет вакансий — предупреждение об этом печатает сам `report`"
            )
        elif applied:
            lines.append(
                "база НЕ ужата — `VACUUM` отказал (см. ОШИБКУ ниже), но уже обнулённые "
                "описания и удалённые файлы это не отменяет"
            )
        for error in self.errors:
            lines.append(f"ОШИБКА: {error}")
        return "\n".join(lines)
