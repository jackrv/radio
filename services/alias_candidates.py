"""Кандидати в алиаси позивних із розмічених записів архіву.

Лише підказка: нічого не записує. Пара «почуте слово → позивний» з’являється,
коли Whisper поставив перед тим самим номером інше слово, ніж оператор
у правильному тексті, або (для позивного без номера) написав схоже слово
замість нього. Записи, де нинішня обробка тексту вже знайшла цей позивний
(нечіткий пошук, номери, чинні алиаси), не рахуються: алиас там нічого не змінить.
Оператор сам вирішує, що додати; збереження перевіряє список
тими самими правилами, що й ручну правку.
"""
from collections import defaultdict

from rapidfuzz import fuzz

from core.callsigns import FOLD, to_items

# Службові слова: як алиас вони зламали б пошук у звичайних фразах.
FUNCTION_WORDS = {"я", "на", "к", "і", "й", "та", "до", "по", "в", "у", "з", "із", "зі", "а", "о", "не",
                  "и", "с", "за", "от", "від", "це", "то", "так", "ну", "ще", "вже", "прийом", "приём",
                  "прийнято", "прийняв", "принято"}
MIN_LENGTH = 3
# Позивний без номера: найсхожіше слово гіпотези, якщо позивного в ній немає.
STANDALONE_MIN_SIMILARITY = 50


def _pairs(items):
    """[(слово, номер)] — слово безпосередньо перед числовою групою."""
    return [(items[i][1], items[i + 1][1]) for i in range(len(items) - 1)
            if items[i][0] == "word" and items[i + 1][0] == "num"]


def _fold(word):
    return word.translate(FOLD)


def collect(records, callsigns, all_names, current_aliases, resolved=None):
    """records: [{"id", "reference", "raw_text"}] — розмічене мовлення.

    all_names — імена всіх словників; current_aliases — {почуте: позивний};
    resolved — {id запису: позивні, які обробка вже знайшла у фінальному тексті}.
    """
    resolved = resolved or {}
    found = defaultdict(lambda: {"count": 0, "records": [], "numbers": set()})

    def add(heard, name, number, record_id):
        entry = found[(heard, name)]
        entry["count"] += 1
        if number:
            entry["numbers"].add(number)
        if record_id not in entry["records"]:
            entry["records"].append(record_id)

    for record in records:
        reference, hypothesis = record.get("reference") or "", record.get("raw_text") or ""
        if not reference.strip() or not hypothesis.strip():
            continue
        ref_items, hyp_items = to_items(reference), to_items(hypothesis)
        by_number = defaultdict(set)
        for word, number in _pairs(ref_items):
            name = callsigns._folded.get(_fold(word))
            if name:
                by_number[number].add(name)
        for heard, number in _pairs(hyp_items):
            names = by_number.get(number)
            # Один номер у різних позивних цього запису — незрозуміло, кого почули.
            if not names or len(names) != 1:
                continue
            name = next(iter(names))
            if _fold(heard) != _fold(name) and name not in resolved.get(record["id"], ()):
                add(heard, name, number, record["id"])

        ref_words = {_fold(v) for kind, v in ref_items if kind == "word"}
        hyp_words = [v for kind, v in hyp_items if kind == "word"]
        hyp_folded = {_fold(w) for w in hyp_words}
        for name in {callsigns._folded.get(f) for f in ref_words} & callsigns.standalone:
            if _fold(name) in hyp_folded:
                continue
            options = [(fuzz.ratio(_fold(w), _fold(name)), w) for w in hyp_words
                       if _fold(w) not in ref_words and len(w) >= MIN_LENGTH]
            if options and name not in resolved.get(record["id"], ()):
                score, heard = max(options)
                if score >= STANDALONE_MIN_SIMILARITY:
                    add(heard, name, None, record["id"])

    targets = defaultdict(set)
    for heard, name in found:
        targets[heard].add(name)

    result = []
    for (heard, name), entry in found.items():
        score = fuzz.ratio(_fold(heard), _fold(name))
        without_number = name in callsigns.standalone or name in callsigns.any_number
        allowed = [n for n in sorted(entry["numbers"]) if n in callsigns.numbers.get(name, ()) or without_number]
        threshold = callsigns.standalone_threshold if name in callsigns.standalone and not allowed else callsigns.threshold
        # Нечіткий пошук уже ловить слово, якщо схожість не нижча за поріг словника.
        recognised = score >= threshold or any(
            n == name for number in allowed for n, num, _ in callsigns.find(f"{heard} {number}"))
        flags = []
        if current_aliases.get(heard) == name:
            flags.append("already_added")
        elif recognised:
            flags.append("already_matched")
        if heard in callsigns.names or (heard in all_names and without_number):
            flags.append("dictionary_word")
        elif heard in all_names:
            flags.append("other_dictionary")
        if len(heard) < MIN_LENGTH or heard in FUNCTION_WORDS:
            flags.append("function_word")
        if len(targets[heard]) > 1:
            flags.append("ambiguous")
        if entry["numbers"] and not allowed:
            # Номера немає у словнику: ні алиас, ні нечіткий пошук його не знайдуть.
            flags.append("number_unknown")
        if name in callsigns.standalone:
            flags.append("no_number")
        result.append({"heard": heard, "callsign": name, "count": entry["count"],
                       "records": entry["records"][:5], "numbers": sorted(entry["numbers"])[:5],
                       "similarity": round(score), "flags": flags,
                       "addable": not ({"already_added", "already_matched", "dictionary_word", "function_word"}
                                       & set(flags))})
    result.sort(key=lambda c: (not c["addable"], -c["count"], c["heard"]))
    return result


def final_callsigns(records, db_path, archive):
    """{id: позивні фінального тексту} — справжня обробка з чинними списками; лише читання.

    Збій (немає таблиць, стара база) — порожній словник: кандидати як раніше, за сирим текстом.
    """
    from unittest.mock import Mock
    from core.text_analysis import analyze_text
    from services.text_replay import load_setup, templates
    try:
        dictionaries, short_garbage, values = load_setup(db_path, archive)
        current = {"phrases": tuple(values.get("phrase_templates", ())),
                   "patterns": tuple(values.get("phrase_patterns", ())),
                   "aliases": (dict(values.get("aliases", {})), dict(values.get("callsign_aliases", {})))}
        result = {}
        with templates(**current):
            for record in records:
                raw = record.get("raw_text") or ""
                if raw.strip():
                    found = analyze_text(raw, *dictionaries, short_garbage, Mock()).callsigns
                    result[record["id"]] = {name for name, *_ in found}
        return result
    except Exception:
        return {}
