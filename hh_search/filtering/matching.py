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

# Границы слова, устойчивые к «+», «#» и «.» — обычный \b на них не работает,
# потому что после «+» нет перехода между словесным и несловесным символом.
_LEFT_BOUNDARY = r"(?<![\w+#.])"
_RIGHT_BOUNDARY = r"(?![\w+#.])"


def normalize(text: str) -> str:
    """Нижний регистр плюс схлопывание визуально одинаковых символов."""
    return text.lower().translate(_TRANSLATION)


def _compile(pattern: str) -> re.Pattern[str]:
    by_stem = bool(_CYRILLIC.search(pattern))
    words = [re.escape(word) for word in normalize(pattern).split()]
    body = r"\s+".join(words)
    tail = r"\w*" if by_stem else _RIGHT_BOUNDARY
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
