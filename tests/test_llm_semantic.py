"""Вектор профиля, косинус и упаковка вектора в BLOB."""

import pytest

from hh_search.config.models import ProfileConfig
from hh_search.llm.semantic import cosine, pack_vector, profile_text, unpack_vector

PROFILE = ProfileConfig.model_validate(
    {
        "weights": {"title": 0.4, "stack": 0.3, "responsibilities": 0.2, "domain": 0.1},
        "saturation": {"stack": 5, "responsibilities": 3},
        "penalty_per_signal": 15,
        "signals": {
            "title_roles": [["team lead", "тимлид"], "ведущ"],
            "title_tech": ["backend", "embedded"],
            "stack": ["yocto", ["arm", "arm64"]],
            "responsibilities": ["архитектур"],
            "domain": ["телеком"],
        },
        "negative": ["курьер"],
        "report_threshold": 60,
    }
)


def test_cosine_of_a_vector_with_itself_is_one() -> None:
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_zero_vector_gives_zero_and_not_a_division_error() -> None:
    """Нулевой вектор — испорченный BLOB, а не осмысленное направление.

    Деление на его норму дало бы `ZeroDivisionError` из середины отчёта:
    исключение, которого нет ни в одном обработчике конвейера, то есть
    одна испорченная строка уронила бы отправку всего отчёта.
    """
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_vectors_of_different_length_are_refused() -> None:
    """Разная размерность — это разные модели, а не повод посчитать по общей части.

    Имя модели рядом с вектором (§5 спеки) ловит смену модели в конфиге, но
    не ловит вектор, записанный наполовину. Молчаливый zip обрезал бы длинный
    до короткого и вернул правдоподобное число.
    """
    with pytest.raises(ValueError, match="размерност"):
        cosine([1.0, 2.0], [1.0, 2.0, 3.0])


def test_pack_and_unpack_round_trip() -> None:
    vector = [0.125, -0.5, 1.0, 0.0]
    assert unpack_vector(pack_vector(vector)) == vector


def test_packed_vector_is_four_bytes_per_value() -> None:
    """float32, а не float64: 1024 значения — 4 КБ на вакансию, а не 8.

    Размер назван в §5 спеки, и он не безразличен: колонка ложится в базу,
    где сейчас ~2.83 КБ на вакансию целиком.
    """
    assert len(pack_vector([1.0] * 1024)) == 4096


def test_blob_of_a_broken_length_is_refused() -> None:
    with pytest.raises(ValueError, match="длин"):
        unpack_vector(b"\x00\x01\x02")


def test_profile_text_carries_every_signal_group() -> None:
    """Текст профиля собирается из ВСЕХ групп сигналов, включая вложенные.

    Вложенный список — группа написаний одной сущности (§6 спеки
    2026-07-27). Взять из неё только первое написание значило бы отдать
    модели профиль беднее того, по которому считается ключевая оценка.
    """
    text = profile_text(PROFILE).lower()

    # По одному сигналу из КАЖДОЙ группы: без «телеком» выпадение целой
    # группы `domain` из перечня не красило ни одного теста (проверено
    # мутацией) — текст профиля молча обеднел бы на весь домен.
    for signal in (
        "team lead",
        "тимлид",
        "ведущ",
        "backend",
        "yocto",
        "arm64",
        "архитектур",
        "телеком",
    ):
        assert signal in text, signal


def test_profile_text_does_not_carry_stop_words() -> None:
    """Стоп-слова в текст профиля не попадают — иначе он тянул бы к отсеянному.

    `negative` описывает то, чего владелец НЕ хочет. Эмбеддинг не знает
    знака: слово «курьер» в строке профиля приблизило бы к нему курьерские
    вакансии, то есть штраф превратился бы в притяжение.
    """
    assert "курьер" not in profile_text(PROFILE).lower()
