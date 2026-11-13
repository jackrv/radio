"""Выделение кандидатов на словесные исправления."""

import re
from difflib import SequenceMatcher

from callsigns import NUMBER_WORDS, to_items


MAX_WORDS_PER_SIDE = 2


def tokenize(text: str) -> list[str]:
    """Разбивает текст на слова без применения алиасов позывных."""
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


def number_groups(text: str) -> list[str]:
    """Возвращает числовые группы в существующем формате Radio 345."""
    return [
        value
        for kind, value in to_items(text)
        if kind == "num"
    ]


def contains_number(tokens: list[str]) -> bool:
    return any(
        token in NUMBER_WORDS
        or any(char.isdigit() for char in token)
        for token in tokens
    )


def extract_word_changes(
    original: str,
    corrected: str,
) -> list[tuple[str, str]]:
    """Выделяет короткие замены для последующей проверки.

    Это кандидаты на обучение, а не разрешённые глобальные правила.
    """
    before = tokenize(original)
    after = tokenize(corrected)

    if not before or not after:
        return []

    # Не учим правила на фразах с изменёнными номерами.
    if number_groups(original) != number_groups(corrected):
        return []

    matcher = SequenceMatcher(
        None, before, after, autojunk=False
    )

    changes = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue

        old = before[i1:i2]
        new = after[j1:j2]

        # Длинные переписывания фраз не превращаем в правила.
        if not (
            1 <= len(old) <= MAX_WORDS_PER_SIDE
            and 1 <= len(new) <= MAX_WORDS_PER_SIDE
        ):
            continue

        # Цифры и числительные не обучаем таким способом.
        if contains_number(old) or contains_number(new):
            continue

        changes.append((
            " ".join(old),
            " ".join(new),
        ))

    return changes
