import math
import re

from callsigns import basic_normalize, normalize, to_items


def fmt(items):
    """Форматирует найденные позывные и команды."""
    return ", ".join(
        f"{name} {number}" if number else name
        for name, number, _ in items
    )


def spoken_number(number):
    """Чотирицифровий код вимовляють парами: 4242 -> «42 42»."""
    return f"{number[:2]} {number[2:]}" if len(number) == 4 and number.isdigit() else number


def fmt_spoken(items):
    """Знайдені команди так, як їх вимовляють: «злива 42 42, грім»."""
    return ", ".join(f"{name} {spoken_number(number)}" if number else name for name, number, _ in items)


_WORD = re.compile(r"[^\W\d_]+(?:[’'ʼ`][^\W\d_]+)*")
_NUMBER_TAIL = re.compile(r"[\s,.;:–—-]*\d[\d\s,.;:–—-]*")


def canonical_text(text, dictionaries):
    """Слово, впізнане разом із точним номером словника, пишеться як у словнику.

    «злата 42 42» -> «злива 42 42» лише тоді, коли саме це слово з саме цим
    номером дало збіг. Номери, решта тексту й незрозумілі шматки не змінюються.
    Слово, яке саме є назвою словника, лишається: його тлумачення видно в шапці.
    """
    known = {name for dictionary in dictionaries for name in dictionary.names}
    out, last = [], 0
    for word in _WORD.finditer(text):
        tail = _NUMBER_TAIL.match(text, word.end())
        if not tail:
            continue
        heard = basic_normalize(word.group())
        snippet = text[word.start():tail.end()]
        hit = None
        for dictionary in dictionaries:
            hit = next((m for m in dictionary.matches(snippet) if m.exact), None)
            if hit:
                break
        if not hit or hit.heard != heard or heard in known:
            continue
        name = hit.name[:1].upper() + hit.name[1:] if word.group()[:1].isupper() else hit.name
        out += [text[last:word.start()], name]
        last = word.end()
    return "".join(out) + text[last:]


GARBAGE_DEFAULTS = {
    "min_words": 12,
    "min_unique_ratio": 0.25,
    "max_words_per_s": 5.0,
    "extra_words": 3,
}


def looks_like_garbage(text: str, duration_s: float, *, min_words=12, min_unique_ratio=0.25,
                       max_words_per_s=5.0, extra_words=3) -> bool:
    """Отсекает длинные фрагменты с признаками мусорного распознавания.

    Пороги редагуються в локальній панелі (runtime_settings) без перезапуску.
    """
    tokens = normalize(text).split()

    if len(tokens) < min_words:
        return False

    if len(set(tokens)) / len(tokens) < min_unique_ratio:
        return True

    return len(tokens) > duration_s * max_words_per_s + extra_words


def is_short_garbage(text: str, short_garbage: set[str]) -> bool:
    """Проверяет известные короткие ложные распознавания."""
    return normalize(text) in short_garbage


def is_low_confidence_short(text: str, segments, callsigns, commands, *, max_words=2, logprob=-0.7) -> bool:
    """Короткий текст, у якому Whisper не впевнений: найчастіше вигадка на шумі.

    ``segments`` — сегменти Whisper з ``avg_logprob``; береться найнижчий. Без
    оцінок правило не діє. Команда (будь-яка) чи позивний із номером захищають
    фразу: «Дуб 42» з низькою впевненістю важливіше за пропущене сміття.
    """
    words = re.findall(r"[^\W_]+", text or "")
    if not words or len(words) > max_words:
        return False
    scores = [s.get("avg_logprob") for s in segments or () if isinstance(s, dict)]
    scores = [s for s in scores if isinstance(s, (int, float)) and math.isfinite(s)]
    if not scores or min(scores) >= logprob:
        return False
    if commands.find(text) or any(num for _name, num, _score in callsigns.find(text)):
        return False
    return True


def is_bare_callsign(text: str, cs_found, callsigns) -> bool:
    """Лише позивні без номера й нічого більше: «Дуб», «дуб, дуб».

    Позивний без номера в ефірі завжди йде з командою або текстом, тож саме слово —
    це найчастіше вигадка Whisper (ім’я з підказки на звуку без мовлення).
    """
    if not cs_found or any(item[1] for item in cs_found):
        return False
    words = normalize(text).split()
    return bool(words) and all(
        not word.isdigit() and any(item[1] is None for item in callsigns.find(word)) for word in words)


_SELF = re.compile(r"(?<![\w’'ʼ`])я(?![\w’'ʼ`])", re.IGNORECASE)


def _header_callsigns(text, callsigns):
    """Позивні на самому початку фрази, до першого слова, що не є позивним.

    Повертає (позивні, чи йдуть одразу за ними цифри повідомлення). Кома
    між групами зберігається: «56, 17, 76» — три групи, а не одне число.
    """
    items = to_items(text)
    end = 0
    while end < len(items) and (items[end][0] == "num" or callsigns.name_of(items[end][1])):
        end += 1
    header = lambda n: callsigns.find(", ".join(value for _, value in items[:n]))
    found = header(end)
    message_digits = end > 0 and items[end - 1][0] == "num" and header(end - 1) == found
    return found, message_digits


def split_roles(text: str, callsigns):
    """Разделяет найденные позывные на адресата и отправителя.

    З «я»: усе до «я» — адресати, перший позивний після — відправник.
    Без «я» — лише форма «К, (К,) О, текст»: рівно два різні позивні підряд
    на початку, далі слова, а не цифри, і більше жодного позивного у фразі.
    Тоді перший — адресат, другий (лише з номером) — відправник. Решта (один позивний, кілька
    адресатів підряд, «К1, К2, 17 76», злиті передачі «О, текст, О, текст») —
    адресати без відправника, як раніше.
    """
    # Ділимо сам текст, а не слова без розділових знаків: інакше «я дуб 700, 17 86»
    # злилося б у «700 17 86» -> одне число 7001786 і відправника не знайшлося б.
    marker = _SELF.search(text)
    if marker:
        to_found = callsigns.find(text[:marker.start()])
        from_found = callsigns.find(text[marker.end():])[:1]
        return to_found, from_found

    found = callsigns.find(text)
    if len(found) == 2:
        header, message_digits = _header_callsigns(text, callsigns)
        # «К1, К2, 17 76» — частіше розсилка двом адресатам, ніж «К, О»: ролі не вгадуємо.
        # Позивний без номера Whisper часто вигадує з підказки («…, вишня, …»):
        # відправником стає лише позивний із номером.
        if header == found and not message_digits and found[1][1]:
            return found[:1], found[1:]
    return found, []


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

    if len(corrected_words) != len(original_words):
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
