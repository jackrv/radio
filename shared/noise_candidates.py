"""Кандидати в «Короткий шум» і «Фрази-галюцинації» з розмічених записів архіву.

Лише підказка: нічого не записує. Кандидат — текст Whisper, який повторюється
на записах, позначених оператором як без мовлення (шум ефіру, кімната, сигнал).
Небезпечні кандидати позначаються: текст із числом, словникове ім’я в ньому,
або ті самі слова в справжньому мовленні — такий рядок відкидав би повідомлення.
Збереження все одно перевіряє список тими самими правилами, що й ручну правку.
"""
import re
from collections import defaultdict

from rapidfuzz import fuzz

from shared.callsigns import FOLD

NON_SPEECH = ("noise", "room", "notification")
SPEECH = ("speech", "unclear")
NEAR_NAME = 80           # слово, схоже на назву зі словника («маган» ~ позивний) — не пропонуємо
SHORT_WORDS = 3          # до трьох слів — «Короткий шум», довше — «Фрази-галюцинації»
MIN_COUNT = 2


def key(text):
    """Ключ, як у приймача: слова нормалізованого тексту без розділових знаків."""
    from shared.text_lists import blacklist_key
    return blacklist_key(text or "")


def collect(records, active_keys, dictionary_names, *, min_count=MIN_COUNT):
    """records: [{"label", "status", "raw_text", "reference"}] — розмічені записи.

    active_keys — ключі, які вже є у двох списках; dictionary_names — назви з усіх словників.
    """
    seen = defaultdict(lambda: {"count": 0, "reached_text": 0, "labels": defaultdict(int), "example": ""})
    speech_heard, speech_texts = set(), []
    for record in records:
        heard = key(record.get("raw_text"))
        if record.get("label") in SPEECH:
            if heard:
                speech_heard.add(heard)
            speech_texts.append(" " + key(record.get("reference")) + " ")
            continue
        if record.get("label") not in NON_SPEECH or not heard:
            continue
        item = seen[heard]
        item["count"] += 1
        item["labels"][record["label"]] += 1
        item["reached_text"] += record.get("status") == "ready"
        item["example"] = item["example"] or (record.get("raw_text") or "").strip()
    items = []
    for text, item in seen.items():
        if item["count"] < min_count:
            continue
        words = text.split()
        flags = []
        if text in active_keys:
            flags.append("already_added")
        if any(word.isdigit() for word in words) or re.search(r"\d", text):
            flags.append("has_number")
        if any(word in dictionary_names for word in words):
            flags.append("dictionary_word")
        elif any(fuzz.ratio(word.translate(FOLD), name.translate(FOLD)) >= NEAR_NAME
                 for word in words for name in dictionary_names):
            flags.append("near_name")
        if text in speech_heard or any(f" {text} " in reference for reference in speech_texts):
            flags.append("in_speech")
        items.append({
            "text": text, "example": item["example"], "count": item["count"],
            "reached_text": item["reached_text"], "labels": dict(item["labels"]),
            "list": "short_garbage" if len(words) <= SHORT_WORDS else "blacklist",
            "flags": flags, "addable": not flags,
        })
    items.sort(key=lambda i: (not i["addable"], -i["reached_text"], -i["count"], i["text"]))
    return items
