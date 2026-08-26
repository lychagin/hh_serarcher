"""Семантическая близость вакансии к профилю: текст, косинус, упаковка.

Ходов в сеть здесь нет ни одного — только чистые функции. Вектора
приносит `pipeline/llm_enrich.py`, а этот модуль обязан считаться и на
машине без Ollama: `rescore` и отправка отчёта зовут его в прогонах, где
модель недоступна (§4 спеки
`docs/superpowers/specs/2026-08-26-local-llm-design.md`).
"""

import struct
from collections.abc import Sequence

from hh_search.config.models import ProfileConfig

# Формат упаковки вектора в BLOB: float32, порядок байт задан ЯВНО.
# Нативный порядок сделал бы файл базы непереносимым между машинами, а
# база — это том, который переезжает вместе с сервисом. float32, а не
# float64: bge-m3 отдаёт 1024 значения, то есть 4 КБ на вакансию против
# восьми, при базе с ~2.83 КБ на вакансию целиком.
_ITEM = "<f"
_ITEM_SIZE = struct.calcsize(_ITEM)

# Подписи групп сигналов в тексте профиля. Не украшение: bge-m3 кодирует
# СМЫСЛ строки, и голое перечисление «yocto, arm, телеком» без указания,
# чем эти слова друг другу приходятся, кодируется хуже связного описания.
_GROUP_LABELS = (
    ("title_roles", "Роли"),
    ("title_tech", "Технологии в заголовке"),
    ("stack", "Стек"),
    ("responsibilities", "Обязанности"),
    ("domain", "Домен"),
)


def profile_text(profile: ProfileConfig) -> str:
    """Строка, которую эмбеддим как «чего хочет владелец».

    Собирается из `signals`, и берутся ВСЕ написания каждой группы:
    вложенный список — это группа написаний одной сущности (§6 спеки
    2026-07-27), и взять из неё только первое значило бы отдать модели
    профиль беднее того, по которому считается ключевая оценка.

    `negative` сюда НЕ попадает, и это важнее всего остального в функции.
    Эмбеддинг не знает знака: слово «курьер», описывающее то, чего владелец
    не хочет, приблизило бы к профилю курьерские вакансии. Штраф
    превратился бы в притяжение — молча и с правдоподобными числами.
    """
    lines = []
    for field, label in _GROUP_LABELS:
        groups: list[list[str]] = getattr(profile.signals, field)
        spellings = [spelling for group in groups for spelling in group]
        lines.append(f"{label}: {', '.join(spellings)}.")
    return "\n".join(lines)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Косинус между векторами. Нулевой вектор даёт 0.0, а не исключение.

    Нулевой вектор — это испорченный BLOB, а не осмысленное направление, и
    `ZeroDivisionError` из середины отправки отчёта уронил бы её целиком:
    такого исключения нет ни в одном обработчике конвейера.
    """
    if len(left) != len(right):
        raise ValueError(
            f"векторы разной размерности: {len(left)} и {len(right)} — "
            "это разные модели или запись, оборванная на середине"
        )
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return float(dot / (left_norm * right_norm))


def pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    """BLOB обратно в вектор. Длина не кратна четырём — это порча.

    Молчаливое отбрасывание хвоста дало бы вектор на одно значение короче
    профильного, а разную размерность ловит `cosine`. Сказать об этом
    здесь — значит назвать настоящую причину (оборванная запись), а не ту,
    что видна дальше по цепочке (несовпадение размерности).
    """
    remainder = len(blob) % _ITEM_SIZE
    if remainder:
        raise ValueError(
            f"длина вектора {len(blob)} байт не кратна {_ITEM_SIZE}: "
            f"запись оборвана на {remainder} байтах"
        )
    return [value for (value,) in struct.iter_unpack(_ITEM, blob)]
