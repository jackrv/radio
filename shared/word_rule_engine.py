"""Применение обученных правил с защитой словарей и номеров."""

from shared.word_replacements import apply_general_word_rules, apply_word_rules
from shared.word_rule_validation import (
    filter_dictionary_word_rules,
    filter_general_word_rules,
)
from shared.word_store import load_active_word_rules, load_approved_word_rules


def apply_verified_word_rules(
    text: str,
    db_path,
    dictionaries,
) -> str:
    """Исправляет слова только при разрешённом номере позывного."""

    dictionaries = tuple(dictionaries)

    rules = load_active_word_rules(db_path)

    safe_rules = filter_dictionary_word_rules(
        rules,
        dictionaries,
    )

    # Звичайні слова: лише з окремим дозволом оператора, імена словників не зачіпаються.
    general_rules = filter_general_word_rules(load_approved_word_rules(db_path), dictionaries)
    if general_rules:
        text = apply_general_word_rules(text, general_rules)

    if not safe_rules:
        return text

    def allow_match(original, corrected, number):
        for dictionary in dictionaries:
            numbers = getattr(dictionary, "numbers", {})
            any_number = getattr(dictionary, "any_number", set())

            if number in numbers.get(corrected, ()):
                return True

            if corrected in any_number:
                return True

        return False

    return apply_word_rules(
        text,
        safe_rules,
        allow_match=allow_match,
    )
