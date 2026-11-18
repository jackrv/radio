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


def load_records(store, days=0):
    """Розмічене мовлення з архіву для analyse(): тексти, тривалість, гучність, упевненість Whisper.

    days=0 — увесь архів; інакше — записи за останні days діб. Лише читає.
    """
    import json
    import time
    since = time.time() - max(1, min(days, 365)) * 86400 if days else 0
    with store.connect() as db:
        rows = db.execute("SELECT id, created, reference, data FROM recordings WHERE label='speech' AND reference IS NOT NULL "
                          "AND created >= ? ORDER BY created DESC", (since,)).fetchall()
    records = []
    for record_id, created, reference, data in rows:
        data = json.loads(data)
        signal = data.get("signal") or {}
        segments = [s for s in data.get("whisper_segments") or [] if s.get("avg_logprob") is not None]
        records.append({"id": record_id, "created": created, "reference": reference,
                        "raw_text": data.get("raw_text", ""), "final_text": data.get("final_text", ""),
                        "duration": signal.get("duration_s"), "loudness": signal.get("rms_dbfs"),
                        # Упевненість Whisper — середня ймовірність слів по сегментах (ближче до 0 — упевненіше).
                        "confidence": sum(s["avg_logprob"] for s in segments) / len(segments) if segments else None})
    return records


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


# Умови запису: межі груп і підписи (тривалість, гучність).
DURATION = ((2, "до 2 с"), (5, "2–5 с"), (10, "5–10 с"), (float("inf"), "понад 10 с"))
LOUDNESS = ((-38, "дуже тихо (нижче −38 dBFS)"), (-30, "тихо (−38…−30)"), (-22, "нормально (−30…−22)"), (float("inf"), "гучно (вище −22)"))


def _group(rows, key, bounds):
    groups = []
    for limit, title in bounds:
        lower = groups[-1]["_limit"] if groups else float("-inf")
        part = [r for r in rows if r[key] is not None and lower <= r[key] < limit]
        groups.append({"_limit": limit, "title": title, **_stats(part)})
    return [{k: v for k, v in g.items() if k != "_limit"} for g in groups if g["records"]]


def _stats(rows):
    if not rows:
        return {"records": 0, "no_edits": 0, "share": None, "errors_mean": None, "ids": []}
    clean = sum(r["errors"] == 0 for r in rows)
    return {"records": len(rows), "no_edits": clean, "share": clean / len(rows),
            "errors_mean": sum(r["errors"] for r in rows) / len(rows),
            "ids": [r["id"] for r in rows if r["errors"]][:EXAMPLES]}


def conditions(rows):
    """«Без правок» за тривалістю, гучністю й упевненістю Whisper (чверті за впевненістю)."""
    confident = sorted((r for r in rows if r["confidence"] is not None), key=lambda r: r["confidence"])
    quarters = []
    if len(confident) >= 8:
        size = len(confident) / 4
        titles = ("найменш упевнені 25 %", "нижче середнього", "вище середнього", "найбільш упевнені 25 %")
        for q, title in enumerate(titles):
            part = confident[round(q * size):round((q + 1) * size)]
            quarters.append({"title": title, "range": [part[0]["confidence"], part[-1]["confidence"]], **_stats(part)})
    # Чи передбачає впевненість правки: різниця «без правок» між найвпевненішою й найменш упевненою чвертю.
    separation = quarters[-1]["share"] - quarters[0]["share"] if quarters else None
    return {"duration": _group(rows, "duration", DURATION), "loudness": _group(rows, "loudness", LOUDNESS),
            "confidence": quarters, "separation": separation}


def callsign_accuracy(records, names):
    """По кожному позивному з еталонів: скільки разів почуто вірно, з чим плутається; вигадані позивні."""
    stats, invented = {}, Counter()
    for item in records:
        ref, hyp = metric_words(item["reference"]), metric_words(item.get("final_text"))
        for tag, r1, r2, h1, h2 in Levenshtein.opcodes(ref, hyp):
            ref_part, hyp_part = ref[r1:r2], hyp[h1:h2]
            for token in ref_part:
                if token not in names:
                    continue
                entry = stats.setdefault(token, {"name": token, "total": 0, "correct": 0, "missing": 0, "heard": Counter(), "ids": []})
                entry["total"] += 1
                if tag == "equal":
                    entry["correct"] += 1
                    continue
                if tag == "delete":
                    entry["missing"] += 1
                else:
                    entry["heard"][" ".join(hyp_part)] += 1
                if item["id"] not in entry["ids"] and len(entry["ids"]) < EXAMPLES:
                    entry["ids"].append(item["id"])
            if tag in ("insert", "replace"):
                for token in hyp_part:
                    if token in names and token not in ref_part:
                        invented[token] += 1   # позивного в цьому місці не казали — Whisper узяв його з підказки чи вигадав
    out = []
    for entry in stats.values():
        out.append({**{k: v for k, v in entry.items() if k != "heard"}, "share": entry["correct"] / entry["total"],
                    "heard": [{"text": t, "count": n} for t, n in entry["heard"].most_common(3)],
                    "invented": invented.get(entry["name"], 0)})
    out.sort(key=lambda e: (-e["total"], e["name"]))
    only_invented = [{"name": n, "count": c} for n, c in invented.most_common() if n not in stats]
    return {"callsigns": out, "invented_only": only_invented}


def analyse(records, names):
    """records: [{id, reference, raw_text, final_text, duration?, loudness?, confidence?}] — лише розмічене мовлення."""
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
    # Помилки в словах на фразу (як у WER) — для «майже без правок» і умов запису.
    rows = [{"id": item["id"], "errors": Levenshtein.distance(metric_words(item["reference"]), metric_words(item.get("final_text"))),
             "duration": item.get("duration"), "loudness": item.get("loudness"), "confidence": item.get("confidence")}
            for item in records]
    one = [r["id"] for r in rows if r["errors"] == 1]
    effort = {"no_edits": sum(r["errors"] == 0 for r in rows), "one_word": len(one), "one_word_ids": one[:EXAMPLES],
              "errors_mean": sum(r["errors"] for r in rows) / len(rows) if rows else None}
    return {"records": len(records), "with_edits": with_edits, "categories": categories, "effort": effort,
            "conditions": conditions(rows), **callsign_accuracy(records, names),
            "processing": {"better": len(better), "worse": len(worse), "better_ids": better[:EXAMPLES], "worse_ids": worse[:EXAMPLES]},
            "substitutions": top,
            "inserted": [{"word": w, "count": n} for w, n in inserted.most_common(10) if n >= 2],
            "deleted": [{"word": w, "count": n} for w, n in deleted.most_common(10) if n >= 2]}
