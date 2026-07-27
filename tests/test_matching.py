from hh_search.filtering.matching import SignalMatcher, normalize


def test_normalize_unifies_cyrillic_and_latin_lookalikes() -> None:
    assert normalize("1С") == normalize("1C")  # первая С кириллическая


def test_normalize_collapses_yo_and_case() -> None:
    assert normalize("Стажёр") == normalize("стажер")


def test_latin_pattern_matches_whole_word_only() -> None:
    matcher = SignalMatcher(["lead"])
    assert matcher.find("Ищем Team Lead в команду") == ["lead"]
    assert matcher.find("Strong leadership skills") == []


def test_cyrillic_pattern_matches_by_stem() -> None:
    matcher = SignalMatcher(["архитектур"])
    assert matcher.find("Проектирование архитектуры сервисов") == ["архитектур"]


def test_cyrillic_stem_must_start_a_word() -> None:
    matcher = SignalMatcher(["строй"])
    assert matcher.find("настройка сервера") == []


def test_plus_signs_do_not_break_boundaries() -> None:
    matcher = SignalMatcher(["c++"])
    assert matcher.find("Требуется опыт C++ и Python") == ["c++"]


def test_bare_c_does_not_match_cpp() -> None:
    matcher = SignalMatcher(["c#"])
    assert matcher.find("Требуется опыт C++") == []


def test_stop_word_matches_cyrillic_spelling_of_1c() -> None:
    matcher = SignalMatcher(["1c"])
    assert matcher.find("Разработчик 1С") == ["1c"]


def test_multiword_pattern_tolerates_extra_whitespace() -> None:
    matcher = SignalMatcher(["team lead"])
    assert matcher.find("Ищем Team  Lead") == ["team lead"]


def test_find_returns_original_spelling_without_duplicates() -> None:
    matcher = SignalMatcher(["Yocto", "BSP"])
    assert matcher.find("Yocto, BSP и снова yocto") == ["Yocto", "BSP"]


def test_has_any_is_true_when_something_matched() -> None:
    matcher = SignalMatcher(["junior"])
    assert matcher.has_any("Junior developer") is True
    assert matcher.has_any("Senior developer") is False


# --- Раунд исправлений 1 ---------------------------------------------------


def test_whole_word_pattern_matches_at_end_of_sentence() -> None:
    # Ранее точка справа безусловно блокировала границу слова, из-за чего
    # слово в конце предложения (самый частый случай в текстах вакансий)
    # не совпадало.
    assert SignalMatcher(["python"]).find("Требуется Python.") == ["python"]
    assert SignalMatcher(["lead"]).find("Ищем опытного Team Lead.") == ["lead"]
    assert SignalMatcher(["django"]).find("Опыт: Python. Знание Django.") == [
        "django"
    ]


def test_dotted_suffix_still_excluded_from_whole_word_match() -> None:
    # Должно сохраниться: точка перед следующим словом остаётся границей,
    # иначе "node" ловил бы "node.js", а "c" — "c++".
    assert SignalMatcher(["node"]).find("Опыт с node.js") == []
    assert SignalMatcher(["c"]).find("Требуется опыт C++") == []


def test_multiword_cyrillic_stem_declines_every_word() -> None:
    # Раньше \w* приклеивался только к последнему слову фразы, поэтому
    # промежуточные слова требовали точного совпадения без учёта склонения.
    matcher = SignalMatcher(["информацион безопасн"])
    assert matcher.find("информационной безопасности данных") == [
        "информацион безопасн"
    ]


def test_cyrillic_word_with_digit_requires_whole_word_match() -> None:
    # Кириллица + цифра в одном слове больше не даёт неограниченный
    # префиксный матч: "1С" не должно ловить "1Cats" или "1club".
    matcher = SignalMatcher(["1С"])
    assert matcher.find("продукт 1Cats на рынке") == []
    assert matcher.find("состав команды 1club") == []
    # При этом сходимость кириллической и латинской орфографии сохраняется.
    assert matcher.find("Разработчик 1С") == ["1С"]
