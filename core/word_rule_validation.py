"""Проверка словесных правил по рабочим словарям Radio 345."""

from core.word_learning import (
    tokenize,
    contains_number,
    MAX_WORDS_PER_SIDE,
)


def filter_dictionary_word_rules(
    rules,
    dictionaries,
) -> list[tuple[str, str]]:
    """Оставляет правила с известным правильным названием.

    Правильно написанные названия запрещено заменять.
    """

    known_names = {
        tuple(tokenize(name))
        for dictionary in dictionaries
        for name in getattr(dictionary, "names", ())
        if tokenize(name)
    }

    result = []

    for original, corrected in rules:
        old = tokenize(original)
        new = tokenize(corrected)

        if not (
            1 <= len(old) <= MAX_WORDS_PER_SIDE
            and 1 <= len(new) <= MAX_WORDS_PER_SIDE
        ):
            continue

        if contains_number(old) or contains_number(new):
            continue

        if old == new:
            continue

        # Правильное словарное название не заменяем.
        if tuple(old) in known_names:
            continue

        # Исправленный вариант должен быть известен.
        if tuple(new) not in known_names:
            continue

        result.append((original, corrected))

    return result
