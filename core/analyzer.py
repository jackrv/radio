from callsigns import normalize, to_items


def fmt(items):
    """Форматирует найденные позывные и команды."""
    return ", ".join(
        f"{name} {number}" if number else name
        for name, number, _ in items
    )


def looks_like_garbage(text: str, duration_s: float) -> bool:
    """Отсекает длинные фрагменты с признаками мусорного распознавания."""
    tokens = normalize(text).split()

    if len(tokens) < 12:
        return False

    if len(set(tokens)) / len(tokens) < 0.25:
        return True

    return len(tokens) > duration_s * 5 + 3


def is_short_garbage(text: str, short_garbage: set[str]) -> bool:
    """Проверяет известные короткие ложные распознавания."""
    return normalize(text) in short_garbage


def split_roles(text: str, callsigns):
    """Разделяет найденные позывные на адресата и отправителя."""
    tokens = normalize(text).split()

    if "я" in tokens:
        i = tokens.index("я")
        to_found = callsigns.find(" ".join(tokens[:i]))
        from_found = callsigns.find(" ".join(tokens[i + 1:]))[:1]
        return to_found, from_found

    return callsigns.find(text), []


def correction_is_safe(
    original: str,
    corrected: str,
    dictionaries: tuple,
) -> bool:
    """Не принимает исправление LLM, если оно теряет важные данные."""
    if not corrected or not corrected.strip():
        return False

    original_items = to_items(original)
    corrected_items = to_items(corrected)

    original_numbers = [
        value for kind, value in original_items if kind == "num"
    ]
    corrected_numbers = [
        value for kind, value in corrected_items if kind == "num"
    ]

    if original_numbers != corrected_numbers:
        return False

    original_words = [
        value for kind, value in original_items if kind == "word"
    ]
    corrected_words = [
        value for kind, value in corrected_items if kind == "word"
    ]

    if len(corrected_words) < len(original_words):
        return False


    # Защищаем уже правильно написанные словарные названия.
    for dictionary in dictionaries:
        for name in getattr(dictionary, "names", ()):
            canonical = to_items(name)
            if not canonical:
                continue

            size = len(canonical)

            present_before = any(
                original_items[j:j + size] == canonical
                for j in range(len(original_items) - size + 1)
            )

            present_after = any(
                corrected_items[j:j + size] == canonical
                for j in range(len(corrected_items) - size + 1)
            )

            if present_before and not present_after:
                return False

    for dictionary in dictionaries:
        before = {
            (name, number): score
            for name, number, score in dictionary.find(original)
        }
        after = {
            (name, number): score
            for name, number, score in dictionary.find(corrected)
        }

        # Проверяем словарные названия независимо от номеров.
        # find() может не обнаружить название с неверным номером.
        from callsigns import FOLD

        original_folded = [
            value.translate(FOLD)
            for kind, value in original_items
            if kind == "word"
        ]
        corrected_folded = [
            value.translate(FOLD)
            for kind, value in corrected_items
            if kind == "word"
        ]

        for name in getattr(dictionary, "names", ()):
            canonical = [
                value.translate(FOLD)
                for kind, value in to_items(name)
                if kind == "word"
            ]

            if not canonical:
                continue

            size = len(canonical)

            present_before = any(
                original_folded[j:j + size] == canonical
                for j in range(len(original_folded) - size + 1)
            )

            present_after = any(
                corrected_folded[j:j + size] == canonical
                for j in range(len(corrected_folded) - size + 1)
            )

            # Разрешаем исправление ранее найденного позывного,
            # например его известного алиаса.
            recognized_before = any(
                found_name == name
                for found_name, number in before
            )

            if (
                present_after
                and not present_before
                and not recognized_before
            ):
                return False

        # LLM не должна создавать новые команды или позывные.
        for key in after:
            if key not in before:
                return False

        for key, old_score in before.items():
            if key not in after or after[key] < old_score:
                return False

    return True
