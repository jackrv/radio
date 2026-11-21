"""Проверка словесных правил по рабочим словарям Radio 345."""

import re
from core.callsigns import FOLD
from core.word_learning import (
    tokenize,
    contains_number,
    similarity,
    MAX_WORDS_PER_SIDE,
    PAIR_SIMILARITY,
)

MIN_WORD_LENGTH = 3
NEAR_NAME_SIMILARITY = 80.0
# Різна кількість слів — лише склеювання/розбиття того самого написання («на бат» → «набат»).
SPLIT_MERGE_SIMILARITY = 85.0
# Службові слова ефіру: їх майже завжди почуто правильно, тож навчена заміна їх на інше слово зіпсувала б
# більшість фраз (18.11.2026: «прийом» → «прийняв» — 7 фраз краще, 204 гірше; «без правок» 45 → 16).
SERVICE_WORDS = frozenset({"прийом", "прийнято", "прийняв", "прийняла", "так", "ні", "добре", "я", "на", "до",
                           "розбираю", "чую", "звʼязку", "звʼязок", "посилку", "отримав", "роботу", "роботи"})
SERVICE_PATTERN = re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(w) for w in SERVICE_WORDS)+r')(?!\w)')


def known_name_tuples(dictionaries) -> frozenset:
    return frozenset(
        tuple(tokenize(name))
        for dictionary in dictionaries
        for name in getattr(dictionary, "names", ())
        if tokenize(name)
    )


def classify_pair(original, corrected, names) -> tuple[str, str | None]:
    """Клас пари: callsign / word (голос), review (перевірка) або excluded.

    callsign — виправлення на відоме словникове ім’я; застосовується лише перед
    дозволеним номером. word — звичайне слово; застосовується тільки після
    підтверджень і окремого дозволу оператора.
    """
    old, new = tuple(tokenize(original)), tuple(tokenize(corrected))
    if not old or not new or old == new:
        return "excluded", "format"
    if not (len(old) <= MAX_WORDS_PER_SIDE and len(new) <= MAX_WORDS_PER_SIDE):
        return "excluded", "long_rewrite"
    if contains_number(list(old)) or contains_number(list(new)):
        return "excluded", "number"
    if len(old) == 1 and len(old[0]) < MIN_WORD_LENGTH:
        return "excluded", "short_word"
    # Зберегти дозволене склеювання «на бат» → «набат», але не дозволити
    # зміну службового слова всередині складеної заміни. Токени апострофів
    # у tokenize() розділяються, тому перевіряємо також ціле написання.
    source = original.casefold().replace("'", "ʼ").replace("’", "ʼ").replace("`", "ʼ")
    if "".join(old) != "".join(new) and SERVICE_PATTERN.search(source):
        return "excluded", "service_word"
    if old in names and new in names:
        return "excluded", "callsign_swap"
    if old in names:
        return "excluded", "known_name"
    if len(old) != len(new) and similarity(" ".join(old), " ".join(new)) < SPLIT_MERGE_SIMILARITY:
        # Інакше в правило потрапило б додане чи видалене слово («пазіцію» → «нашу позицію»).
        return "review", "word_count"
    if new in names:
        return "callsign", None
    if any(len(token) < MIN_WORD_LENGTH for token in old + new) and len(old) == len(new) == 1:
        return "excluded", "short_word"
    if similarity(" ".join(old), " ".join(new)) < PAIR_SIMILARITY:
        return "review", "dissimilar"
    folded = [" ".join(name).translate(FOLD) for name in names]
    probe = " ".join(old).translate(FOLD)
    if any(similarity(probe, name) >= NEAR_NAME_SIMILARITY for name in folded):
        return "review", "near_callsign"
    return "word", None


def filter_general_word_rules(rules, dictionaries) -> list[tuple[str, str]]:
    """Звичайні слова: жодна сторона не є словниковим ім’ям, без чисел і коротких слів."""
    names = known_name_tuples(dictionaries)
    result = []
    for original, corrected in rules:
        kind, _ = classify_pair(original, corrected, names)
        if kind in ("word", "review"):
            # review — правило, яке оператор підтвердив і дозволив вручну.
            result.append((original, corrected))
    return result


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
