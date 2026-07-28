import pytest

from hh_search.filtering.matching import SignalGroupMatcher, SignalMatcher, normalize


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
    assert SignalMatcher(["django"]).find("Опыт: Python. Знание Django.") == ["django"]


def test_dotted_suffix_still_excluded_from_whole_word_match() -> None:
    # Должно сохраниться: точка перед следующим словом остаётся границей,
    # иначе "node" ловил бы "node.js", а "c" — "c++".
    assert SignalMatcher(["node"]).find("Опыт с node.js") == []
    assert SignalMatcher(["c"]).find("Требуется опыт C++") == []


def test_multiword_cyrillic_stem_declines_every_word() -> None:
    # Раньше \w* приклеивался только к последнему слову фразы, поэтому
    # промежуточные слова требовали точного совпадения без учёта склонения.
    matcher = SignalMatcher(["информацион безопасн"])
    assert matcher.find("информационной безопасности данных") == ["информацион безопасн"]


def test_cyrillic_word_with_digit_requires_whole_word_match() -> None:
    # Кириллица + цифра в одном слове больше не даёт неограниченный
    # префиксный матч: "1С" не должно ловить "1Cats" или "1club".
    matcher = SignalMatcher(["1С"])
    assert matcher.find("продукт 1Cats на рынке") == []
    assert matcher.find("состав команды 1club") == []
    # При этом сходимость кириллической и латинской орфографии сохраняется.
    assert matcher.find("Разработчик 1С") == ["1С"]


# --- Раунд исправлений 3 ---------------------------------------------------


@pytest.mark.parametrize("pattern", ["", "   ", "\t\n"])
def test_blank_pattern_is_rejected(pattern: str) -> None:
    # Пустой паттерн компилировался в границы без тела и совпадал почти с любым
    # текстом: в отсеве это давало необратимый reject с пустой причиной.
    with pytest.raises(ValueError, match="пуст"):
        SignalMatcher([pattern])


def test_blank_pattern_is_rejected_even_next_to_valid_ones() -> None:
    with pytest.raises(ValueError):
        SignalMatcher(["yocto", "", "bsp"])


# --- Раунд исправлений: минимальная длина основы ---------------------------


def test_one_letter_cyrillic_signal_no_longer_matches_by_stem() -> None:
    """`negative: ["с"]` компилировался в `c\\w*` и после схлопывания
    омоглифов совпадал с ЛЮБЫМ словом на латинскую `c` или кириллическую
    `с`. В отсеве это необратимо: вакансия уходит в `rejected` навсегда, а
    в логе остаётся правдоподобная причина «стоп-слово в заголовке: с».
    """
    matcher = SignalMatcher(["с"])
    assert matcher.find("Программист: WinForms (MVP), C#, .NET") == []
    assert matcher.find("Стажёр 1С в отдел корпоративных решений") == []
    # Правило снимает только матч ПО ОСНОВЕ: целым словом сигнал работает
    # по-прежнему, потому что ровно это в конфиге и написано.
    assert matcher.find("Разработчик с опытом Linux") == ["с"]


def test_two_letter_cyrillic_signal_no_longer_matches_by_stem() -> None:
    matcher = SignalMatcher(["по"])
    assert matcher.find("Подготовка и поиск решений, поддержка") == []
    assert matcher.find("Программист на ПО Fansy (SPECTRE, DEPO)") == ["по"]


def test_three_letter_cyrillic_signal_still_matches_by_stem() -> None:
    """Граница проведена по трём символам, а не «по всем коротким»: `ток`
    и `сеть` — законные основы, и отобрать у них склонение значило бы
    чинить одну тихую поломку другой."""
    assert SignalMatcher(["код"]).find("Разработка кода и кодовой базы") == ["код"]


def test_multiword_signal_with_a_short_cyrillic_word_still_matches() -> None:
    """`оператор пк` — живой сигнал образца §7. `пк` короче предела и
    матчится целым словом, но фраза обязана продолжать срабатывать."""
    matcher = SignalMatcher(["оператор пк"])
    assert matcher.find("Оператор ПК") == ["оператор пк"]
    assert matcher.find("Оператора ПК в офис") == ["оператор пк"]


@pytest.mark.parametrize(
    ("signal", "text"),
    [
        ("1c", "Разработчик 1С"),
        ("qa", "QA инженер в команду"),
        ("go", "Разработчик Go и Python"),
        ("c++", "Требуется опыт C++"),
        ("c#", "Программист C# (.NET)"),
    ],
)
def test_short_latin_and_digit_signals_are_untouched_by_the_minimum(signal: str, text: str) -> None:
    """Минимальная длина введена ТОЛЬКО для матча по основе, то есть для
    кириллицы без цифр. Латинские целословные сигналы и коды с цифрой
    короче предела — их поломка означала бы, что лечение хуже болезни."""
    assert SignalMatcher([signal]).find(text) == [signal]


def test_short_latin_signal_still_respects_word_boundaries() -> None:
    assert SignalMatcher(["go"]).find("Опыт работы с google cloud") == []


# --- Раунд исправлений: группы синонимов -----------------------------------


def test_group_of_spellings_is_one_signal() -> None:
    """Правая граница §6.1 обязывает перечислять `arm`, `arm64`, `armv7`,
    `armv8` отдельными паттернами. Считать их четырьмя сигналами значит
    выдавать одно семейство процессоров за четыре технологии."""
    matcher = SignalGroupMatcher([["arm", "arm64", "armv7", "armv8"], ["yocto"]])
    assert matcher.find("Опыт ARM, ARM64, ARMv7 и ARMv8") == ["arm / arm64 / armv7 / armv8"]


def test_group_reports_only_the_spellings_that_actually_matched() -> None:
    """Разбивка остаётся ответом на вопрос «почему 87?»: в ней конкретные
    написания, а не имя группы, которого в конфиге нет."""
    matcher = SignalGroupMatcher([["arm", "arm64", "armv7", "armv8"], ["yocto"]])
    assert matcher.find("Сборка под ARM64") == ["arm64"]


def test_groups_keep_the_order_of_the_config() -> None:
    matcher = SignalGroupMatcher([["node", "node.js", "nodejs"], ["docker"]])
    assert matcher.find("Docker и Node.js в проекте") == ["node.js", "docker"]


def test_group_matcher_finds_nothing_when_no_spelling_matched() -> None:
    matcher = SignalGroupMatcher([["arm", "arm64"]])
    assert matcher.find("Только Python и Django") == []


def test_flat_matcher_is_a_group_per_spelling() -> None:
    """Префильтру нужен плоский список: там каждое стоп-слово само по себе
    причина отказа, и группировать их не во что."""
    matcher = SignalMatcher(["arm", "arm64"])
    assert matcher.find("Опыт ARM и ARM64") == ["arm", "arm64"]
