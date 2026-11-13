"""Безопасное применение подтверждённых словесных замен."""

import re

from core.word_learning import (
    tokenize,
    contains_number,
    MAX_WORDS_PER_SIDE,
)


def apply_word_rules(text: str, rules, *, allow_match=None) -> str:
    """Применяет словесные правила перед цифровыми номерами.

    rules: последовательность (original, corrected).

    Выбор активных правил из SQLite выполняется отдельно.
    """

    mapping = {}
    conflicts = set()

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

        key = " ".join(old)
        value = " ".join(new)

        if key == value:
            continue

        if key in mapping and mapping[key] != value:
            conflicts.add(key)
        else:
            mapping[key] = value

    for key in conflicts:
        mapping.pop(key, None)

    if not mapping:
        return text

    # Более длинные словосочетания имеют приоритет.
    keys = sorted(
        mapping,
        key=lambda key: (len(key.split()), len(key)),
        reverse=True,
    )

    patterns = [
        r"\s+".join(
            re.escape(word) for word in key.split()
        )
        for key in keys
    ]

    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(patterns) + r")"
        r"(?!\w)(?=\s+[0-9]+\b)",
        re.IGNORECASE,
    )

    def replace(match):
        original = match.group(0)
        key = " ".join(tokenize(original))
        replacement = mapping[key]

        if allow_match is not None:
            remaining = text[match.end():]
            number_match = re.match(
                r"\s+([0-9]+)\b", remaining
            )

            if (
                number_match is None
                or not allow_match(
                    key,
                    replacement,
                    number_match.group(1),
                )
            ):
                return original

        if original.isupper():
            return replacement.upper()

        if original[0].isupper():
            return replacement[0].upper() + replacement[1:]

        return replacement

    # Один проход: результат замены повторно не обрабатывается.
    return pattern.sub(replace, text)
