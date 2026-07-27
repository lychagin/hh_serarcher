import re
from collections.abc import Sequence

# Кириллические буквы, неотличимые на вид от латинских. Отображение применяется
# и к тексту, и к паттернам, поэтому русские слова продолжают совпадать друг с
# другом, а «1С» и «1C» сходятся в одну форму.
_CONFUSABLES = {
    "ё": "e", "е": "e", "а": "a", "в": "b", "с": "c", "к": "k",
    "м": "m", "н": "h", "о": "o", "р": "p", "т": "t", "у": "y", "х": "x",
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


def _is_stem_word(word: str) -> bool:
    """Кириллическое слово без цифр склоняется, поэтому матчится по основе.

    Решение принимается по исходному написанию слова, до нормализации.
    Цифры исключены из правила, чтобы короткие коды вроде «1С» не превращались
    в неограниченный префиксный матч (1С не должно ловить 1Cats).
    """
    return bool(_CYRILLIC.search(word)) and not any(ch.isdigit() for ch in word)


def _compile(pattern: str) -> re.Pattern[str]:
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


class SignalMatcher:
    """Ищет в тексте вхождения списка сигналов, возвращая исходные написания."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = list(patterns)
        self._compiled = [_compile(pattern) for pattern in self._patterns]

    def find(self, text: str) -> list[str]:
        haystack = normalize(text)
        return [
            original
            for original, regex in zip(self._patterns, self._compiled, strict=True)
            if regex.search(haystack)
        ]

    def has_any(self, text: str) -> bool:
        haystack = normalize(text)
        return any(regex.search(haystack) for regex in self._compiled)
