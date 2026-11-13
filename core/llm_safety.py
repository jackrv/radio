"""Консервативная проверка текстовых изменений Qwen."""

import re

from callsigns import FOLD


def word_tokens(text: str) -> list[str]:
    """Слова без применения алиасов позывных."""
    return re.findall(
        r"[^\W_]+",
        text.casefold(),
        flags=re.UNICODE,
    )


def is_conservative_llm_rewrite(
    original: str,
    corrected: str,
) -> bool:
    """Разрешает только ограниченные изменения написания.

    Не определяет смысл слов. Непроверенные лексические
    замены отклоняются.
    """
    before = word_tokens(original)
    after = word_tokens(corrected)

    if not before or len(before) != len(after):
        return False

    for old, new in zip(before, after):
        if old == new:
            continue

        # Короткие слова особенно опасны:
        # тут/там, так/ні и т.д.
        if min(len(old), len(new)) < 4:
            return False

        # Разрешаем только различия букв, которые
        # существующая нормализация Radio 345
        # считает эквивалентными.
        if old.translate(FOLD) != new.translate(FOLD):
            return False

    return True
