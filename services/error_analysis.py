"""Розбір помилок: що саме заважає тексту «без правок».

Для кожного розміченого мовлення підсумковий текст вирівнюється з еталоном по словах
(ті самі слова, що й у WER: без регістру й розділових знаків). Розбіжності
розкладаються за видами: номери (окремо — лише інше групування цифр, «4862» замість
«48 62»), позивні зі словника, інші слова, вигадані й пропущені слова. Окремо —
чи обробка після Whisper допомогла або зашкодила фразі, і найчастіші заміни слів.

Нічого не змінює: лише читає архів і словник позивних.
"""
import re
from collections import Counter

from rapidfuzz.distance import Levenshtein

from core.text_metrics import metric_words

EXAMPLES = 20   # ID для «показати в архіві» (пошук приймає до ~200 знаків)
_DIGITS = re.compile(r"\d+\Z")

CATEGORIES = {
    "numbers": "Номери",
    "regroup": "Лише групування цифр",
    "callsigns": "Позивні",
    "words": "Інші слова",
    "inserted": "Вигадані слова",
    "deleted": "Пропущені слова",
    "missed": "Тексту немає зовсім",
}


def callsign_names(db):
    """Назви позивних зі словника (перше слово рядка), без вимкнених записів."""
    names = set()
    for (line,) in db.execute("SELECT raw_line FROM dictionary_entries WHERE dictionary_type='callsigns'"):
        line = (line or "").strip()
        if not line or line.startswith("#"):
            continue
        first = re.split(r"[:\s]", line, maxsplit=1)[0]
        names.update(metric_words(first))
    return names


def _kind(token, names):
    if _DIGITS.match(token):
        return "number"
    return "callsign" if token in names else "word"


def join_digits(tokens):
    """Сусідні числа — одним токеном: «48 62» і «4862» порівнюються як одне число."""
    out = []
    for token in tokens:
        if out and _DIGITS.match(token) and _DIGITS.match(out[-1]):
            out[-1] += token
        else:
            out.append(token)
    return out


def record_errors(reference, hypothesis, names):
    """Види розбіжностей однієї фрази та пари замін (гіпотеза → еталон).

    Види визначаються на словах зі склеєними сусідніми числами; якщо після склеювання
    числа збігаються, а самі слова — ні, це «лише групування цифр».
    """
    words_ref, words_hyp = metric_words(reference), metric_words(hypothesis)
    found, pairs = set(), []
    if not words_hyp:
        return ({"missed"} if words_ref else set()), pairs
    ref, hyp = join_digits(words_ref), join_digits(words_hyp)
    if [t for t in ref if _DIGITS.match(t)] == [t for t in hyp if _DIGITS.match(t)] \
            and [t for t in words_ref if _DIGITS.match(t)] != [t for t in words_hyp if _DIGITS.match(t)]:
        found.add("regroup")
    for tag, r1, r2, h1, h2 in Levenshtein.opcodes(ref, hyp):
        if tag == "equal":
            continue
        ref_part, hyp_part = ref[r1:r2], hyp[h1:h2]
        if tag == "insert":
            found.add("inserted")
        elif tag == "delete":
            found.add("deleted")
        else:
            pairs.append((" ".join(hyp_part), " ".join(ref_part)))
        kinds = {_kind(t, names) for t in ref_part + hyp_part}
        if "number" in kinds:
            found.add("numbers")
        if "callsign" in kinds:
            found.add("callsigns")
        if kinds == {"word"}:   # позивний, почутий як інше слово, — це «Позивні», не «Інші слова»
            found.add("words")
    return found, pairs


def analyse(records, names):
    """records: [{id, reference, raw_text, final_text}] — лише розмічене мовлення."""
    categories = {key: {"title": title, "count": 0, "ids": []} for key, title in CATEGORIES.items()}
    substitutions, sub_ids, sub_kind = Counter(), {}, {}
    inserted, deleted = Counter(), Counter()
    better, worse = [], []
    with_edits = 0
    for item in records:
        reference, final, raw = item["reference"] or "", item.get("final_text") or "", item.get("raw_text") or ""
        found, pairs = record_errors(reference, final, names)
        if found:
            with_edits += 1
        for key in found:
            categories[key]["count"] += 1
            if len(categories[key]["ids"]) < EXAMPLES:
                categories[key]["ids"].append(item["id"])
        for pair in pairs:
            substitutions[pair] += 1
            sub_ids.setdefault(pair, [])
            if len(sub_ids[pair]) < EXAMPLES:
                sub_ids[pair].append(item["id"])
            tokens = pair[0].split() + pair[1].split()
            sub_kind[pair] = ("number" if any(_DIGITS.match(t) for t in tokens)
                              else "callsign" if any(t in names for t in tokens) else "word")
        ref_words, hyp_words = metric_words(reference), metric_words(final)
        for tag, r1, r2, h1, h2 in (Levenshtein.opcodes(ref_words, hyp_words) if hyp_words else []):
            if tag == "insert":
                inserted.update(hyp_words[h1:h2])
            elif tag == "delete":
                deleted.update(ref_words[r1:r2])
        # Обробка (словники, шаблони, правила) порівняно з сирим текстом Whisper.
        ref_list = metric_words(reference)
        raw_errors = Levenshtein.distance(ref_list, metric_words(raw))
        final_errors = Levenshtein.distance(ref_list, hyp_words)
        if final_errors < raw_errors:
            better.append(item["id"])
        elif final_errors > raw_errors:
            worse.append(item["id"])
    top = [{"from": a, "to": b, "count": n, "kind": sub_kind[(a, b)], "ids": sub_ids[(a, b)]}
           for (a, b), n in substitutions.most_common(15) if n >= 2]
    return {"records": len(records), "with_edits": with_edits, "categories": categories,
            "processing": {"better": len(better), "worse": len(worse), "better_ids": better[:EXAMPLES], "worse_ids": worse[:EXAMPLES]},
            "substitutions": top,
            "inserted": [{"word": w, "count": n} for w, n in inserted.most_common(10) if n >= 2],
            "deleted": [{"word": w, "count": n} for w, n in deleted.most_common(10) if n >= 2]}
