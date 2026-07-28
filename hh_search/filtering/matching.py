import re
from collections.abc import Sequence

# Кириллические буквы, неотличимые на вид от латинских. Отображение применяется
# и к тексту, и к паттернам, поэтому русские слова продолжают совпадать друг с
# другом, а «1С» и «1C» сходятся в одну форму.
_CONFUSABLES = {
    "ё": "e",
    "е": "e",
    "а": "a",
    "в": "b",
    "с": "c",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "т": "t",
    "у": "y",
    "х": "x",
}
_TRANSLATION = str.maketrans(_CONFUSABLES)

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

# Границы слова, устойчивые к «+» и «#» — обычный \b на них не работает,
# потому что после «+» нет перехода между словесным и несловесным символом.
# Точка сама по себе границей не считается (иначе «Python.» в конце
# предложения не матчился бы), но точка перед следующим словом — считается
# (иначе «node» ловил бы «node.js»).
_LEFT_BOUNDARY = r"(?<![\w+#.])"
_RIGHT_BOUNDARY = r"(?![\w+#]|\.\w)"


def normalize(text: str) -> str:
    """Нижний регистр плюс схлопывание визуально одинаковых символов."""
    return text.lower().translate(_TRANSLATION)


# Ниже этой длины основа перестаёт быть основой. `negative: ["с"]`
# компилировалось в `(?<![\w+#.])c\w*` и после схлопывания омоглифов
# совпадало с любым словом на латинскую `c` или кириллическую `с` —
# одиннадцать живых заголовков из двадцати уходили в необратимый
# `rejected` с правдоподобной причиной в логе; `по` ловило «поиск»,
# «поддержку» и «подготовку». Три символа выбраны как первая длина, на
# которой префикс уже что-то значит: `пк` из живого сигнала «оператор пк»
# ниже предела и матчится целым словом, оставаясь рабочим.
_MIN_STEM_LENGTH = 3


def _is_stem_word(word: str) -> bool:
    """Кириллическое слово без цифр склоняется, поэтому матчится по основе.

    Решение принимается по исходному написанию слова, до нормализации.
    Цифры исключены из правила, чтобы короткие коды вроде «1С» не превращались
    в неограниченный префиксный матч (1С не должно ловить 1Cats), а слишком
    короткие слова — потому что их «основа» не отличает ничего от всего.
    Слово ниже предела не выбрасывается, а матчится целым словом: в конфиге
    написано именно оно, и молча игнорировать его нельзя.
    """
    return (
        len(word) >= _MIN_STEM_LENGTH
        and bool(_CYRILLIC.search(word))
        and not any(ch.isdigit() for ch in word)
    )


def _compile(pattern: str) -> re.Pattern[str]:
    if not pattern.strip():
        # Пустой паттерн даёт регулярку из одних границ, которая совпадает почти
        # с любым текстом: в отсеве это необратимый reject с пустой причиной.
        # Молчаливое «ловит всё» здесь опаснее падения на старте.
        raise ValueError("пустой сигнал: такой паттерн совпал бы с любым текстом")
    raw_words = pattern.split()
    norm_words = normalize(pattern).split()
    parts = [
        re.escape(norm_word) + (r"\w*" if _is_stem_word(raw_word) else "")
        for raw_word, norm_word in zip(raw_words, norm_words, strict=True)
    ]
    body = r"\s+".join(parts)
    # Хвост из \w* у последнего слова уже сам ограничивает совпадение справа;
    # добавлять _RIGHT_BOUNDARY нужно, только если последнее слово не по основе.
    tail = "" if raw_words and _is_stem_word(raw_words[-1]) else _RIGHT_BOUNDARY
    return re.compile(_LEFT_BOUNDARY + body + tail)


# Разделитель написаний внутри одной сработавшей группы. Совпавшие
# написания склеиваются в ОДИН элемент результата, поэтому длина списка
# равна числу засчитанных сигналов, а сам элемент по-прежнему называет
# конкретные слова, из-за которых сигнал засчитан (спека §6).
GROUP_SEPARATOR = " / "


class SignalGroupMatcher:
    """Ищет в тексте сигналы, каждый из которых задан группой написаний.

    Группа — это одна сущность, записанная несколькими паттернами
    вынужденно: правая граница §6.1 запрещает букву и цифру вплотную,
    поэтому `arm`, `arm64`, `armv7`, `armv8` не сводятся к одному
    паттерну. Считать их четырьмя сигналами значит выдавать одно
    семейство процессоров за четыре разные технологии — насыщение
    §6 считает именно группы.
    """

    def __init__(self, groups: Sequence[Sequence[str]]) -> None:
        self._groups = [list(group) for group in groups]
        self._compiled = [[_compile(pattern) for pattern in group] for group in self._groups]

    def find(self, text: str) -> list[str]:
        """По одному элементу на СОВПАВШУЮ группу, в порядке конфига."""
        haystack = normalize(text)
        found: list[str] = []
        for group, regexes in zip(self._groups, self._compiled, strict=True):
            matched = [
                original
                for original, regex in zip(group, regexes, strict=True)
                if regex.search(haystack)
            ]
            if matched:
                found.append(GROUP_SEPARATOR.join(matched))
        return found

    def has_any(self, text: str) -> bool:
        haystack = normalize(text)
        return any(regex.search(haystack) for group in self._compiled for regex in group)


class SignalMatcher(SignalGroupMatcher):
    """Плоский список сигналов: каждое написание само себе группа.

    Нужен префильтру: там сигнал не участвует ни в каком насыщении, а
    попадает в `reject_reason` как отдельная причина отказа, и склеивать
    причины не во что.
    """

    def __init__(self, patterns: Sequence[str]) -> None:
        super().__init__([[pattern] for pattern in patterns])
