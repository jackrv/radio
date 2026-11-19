"""Повтор текстової обробки на збережених виходах Whisper — без моделей і без зупинки приймача.

Для кожного розміченого запису мовлення береться текст, що надійшов у фінальний
аналіз (пропозиція Qwen, якщо її прийнято, інакше результат словесних правил або
сирий вихід Whisper), і проганяється через `core.text_analysis.analyze_text`
з чинними словниками. Контр-перевірка вимикає по одному текстові правила та
фільтри на тих самих записах і словниках. Архів і events.db відкриваються лише для читання.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock



# WER — та сама метрика, що в архіві й лабораторії (без алиасів і виправлень).
from core.text_metrics import edit_distance as distance, metric_words as words  # noqa: E402
from services.sqlite_connection import read_only


def load_records(archive, *, include_non_speech=False):
    """Розмічене мовлення; за запитом також шум, сигнали й кімната."""
    index = Path(archive) / "index.sqlite3"
    if not index.is_file():
        return []
    db = read_only(index)
    try:
        rows = []
        query = ("SELECT id, reference, data, label FROM recordings "
                 "WHERE reference IS NOT NULL AND label IN ('speech','noise','notification','room')"
                 if include_non_speech else
                 "SELECT id, reference, data FROM recordings WHERE reference IS NOT NULL AND label='speech'")
        for row in db.execute(query):
            record_id, reference, data = row[:3]
            label = row[3] if include_non_speech else "speech"
            d = json.loads(data)
            text = d.get("llm_text") if d.get("llm_accepted") else d.get("before_llm")
            text = text or d.get("filtered_text") or d.get("raw_text") or ""
            if text.strip():
                rows.append((record_id, reference, text, label) if include_non_speech
                            else (record_id, reference, text))
        return rows
    finally:
        db.close()


def load_setup(db_path):
    """(словники, короткий шум, чинні значення списків) з events.db лише для читання."""
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import load_active
    db = read_only(db_path)
    try:
        dictionaries = dictionaries_from_lines(read_dictionary_lines(db))
        values = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='text_list_entries'").fetchone():
            _, values = load_active(db)
        return dictionaries, set(values.get("short_garbage", ())), values
    finally:
        db.close()


@contextmanager
def templates(phrases=None, patterns=None, aliases=None):
    """Тимчасово застосувати обороти й шаблони фраз (None — лишити чинні)."""
    import core.callsigns as callsigns_module
    import core.phrase_patterns as pp
    import core.template_text as tt
    saved = tt.PHRASES, pp.PATTERNS, dict(callsigns_module.CALLSIGN_ALIASES), {k: list(v) for k, v in callsigns_module.ALIASES.items()}
    try:
        if aliases is not None:
            callsigns_module.configure_aliases(*aliases)
        if phrases is not None:
            tt.configure_phrases(phrases)
        if patterns is not None:
            pp.configure_patterns(patterns)
        yield
    finally:
        tt.PHRASES, pp.PATTERNS = saved[0], saved[1]
        callsigns_module.CALLSIGN_ALIASES.clear(); callsigns_module.CALLSIGN_ALIASES.update(saved[2])
        callsigns_module.ALIASES.clear(); callsigns_module.ALIASES.update(saved[3])


def run(rows, dictionaries, short_garbage, blacklist=()):
    """Метрики й вихід кожного запису за поточних текстових правил."""
    from core.text_analysis import analyze_text
    from services.text_lists import blacklist_key
    callsigns, commands, watch = dictionaries
    blocked = {blacklist_key(value) for value in blacklist}
    totals = {"records": 0, "non_speech_records": 0, "words": 0, "errors": 0,
              "callsigns_ref": 0, "callsigns_hit": 0, "callsigns_false": 0,
              "commands_ref": 0, "commands_hit": 0, "commands_false": 0, "no_edits": 0, "missed": 0,
              "false_text": 0, "false_text_by_label": {"noise": 0, "notification": 0, "room": 0}}
    outputs = {}
    for row in rows:
        record_id, reference, text = row[:3]
        label = row[3] if len(row) > 3 else "speech"
        key = blacklist_key(text)
        parts = [blacklist_key(part) for part in re.split(r"[.!?;…]+", text) if blacklist_key(part)]
        is_blacklisted = key in blocked or (len(parts) > 1 and all(part in blocked for part in parts))
        if is_blacklisted:
            out, found_callsigns, found_commands = "", [], []
        else:
            result = analyze_text(text, callsigns, commands, watch, short_garbage, Mock())
            out = "" if result.rejected else result.text
            found_callsigns, found_commands = result.callsigns, result.commands
        errors = distance(words(out), words(reference)) if label == "speech" else 0
        if label == "speech":
            totals["records"] += 1
            totals["words"] += len(words(reference))
            totals["errors"] += errors
            totals["no_edits"] += errors == 0
            totals["missed"] += bool(words(reference)) and not out.strip()
        else:
            totals["non_speech_records"] += 1
            if out.strip():
                totals["false_text"] += 1
                totals["false_text_by_label"][label] += 1
        outputs[record_id] = (errors, out, label)
        for kind, dictionary, found in (("callsigns", callsigns, found_callsigns), ("commands", commands, found_commands)):
            truth = {(n, num) for n, num, _ in dictionary.find(reference)}
            got = {(n, num) for n, num, _ in found}
            totals[f"{kind}_ref"] += len(truth)
            totals[f"{kind}_hit"] += len(truth & got)
            totals[f"{kind}_false"] += len(got - truth)
    totals["wer"] = totals["errors"] / max(1, totals["words"])
    return totals, outputs


def countercheck(archive, db_path, *, show=3):
    """По одному вимикає активні текстові правила на тих самих розмічених фразах."""
    rows = load_records(archive, include_non_speech=True)
    dictionaries, short_garbage, values = load_setup(db_path)
    current = {
        "phrases": tuple(values.get("phrase_templates", ())),
        "patterns": tuple(values.get("phrase_patterns", ())),
        "aliases": (dict(values.get("aliases", {})), dict(values.get("callsign_aliases", {}))),
    }
    active_blacklist = tuple(values.get("blacklist", ()))
    with templates(**current):
        active, current_outputs = run(rows, dictionaries, short_garbage, active_blacklist)
    references = {row[0]: row[1] for row in rows}
    checks = []

    def add_check(kind, label, before, *, garbage=short_garbage, blacklist=active_blacklist):
        with templates(**before):
            without, outputs = run(rows, dictionaries, garbage, blacklist)
        changed, false_text_examples = [], []
        for record_id, (errors, text, label) in outputs.items():
            old_errors, old_text, _ = current_outputs[record_id]
            if text == old_text:
                continue
            item = {"id": record_id, "delta": old_errors - errors, "current": old_text,
                    "without": text, "reference": references[record_id]}
            if label == "speech":
                changed.append(item)
            else:
                false_text_examples.append({**item, "label": label})
        worse = [item for item in changed if item["delta"] > 0]
        better = [item for item in changed if item["delta"] < 0]
        no_edits_delta = active["no_edits"] - without["no_edits"]
        missed_delta = active["missed"] - without["missed"]
        wer_delta = active["errors"] / max(1, active["words"]) - without["errors"] / max(1, without["words"])
        false_text_delta = active["false_text"] - without["false_text"]
        harmful = no_edits_delta < 0 or missed_delta > 0 or wer_delta > 1e-12 or false_text_delta > 0
        helpful = no_edits_delta > 0 or missed_delta < 0 or wer_delta < -1e-12 or false_text_delta < 0
        checks.append({
            "kind": kind, "label": label,
            "effect": "mixed" if harmful and helpful else "harmful" if harmful else "helpful" if helpful else "neutral",
            "changed": len(changed), "worse": len(worse), "better": len(better),
            "no_edits_delta": no_edits_delta, "missed_delta": missed_delta, "wer_delta": wer_delta,
            "false_text_delta": false_text_delta,
            "examples": sorted(worse, key=lambda item: -item["delta"])[:show],
            "false_text_examples": false_text_examples[:show],
        })

    for phrase in current["phrases"]:
        add_check("phrase_template", phrase,
                  {**current, "phrases": tuple(item for item in current["phrases"] if item != phrase)})
    for pattern in current["patterns"]:
        add_check("phrase_pattern", pattern,
                  {**current, "patterns": tuple(item for item in current["patterns"] if item != pattern)})
    for key, target in current["aliases"][0].items():
        aliases = (dict(current["aliases"][0]), dict(current["aliases"][1]))
        aliases[0].pop(key)
        add_check("text_alias", f"{key} → {target}", {**current, "aliases": aliases})
    for key, target in current["aliases"][1].items():
        aliases = (dict(current["aliases"][0]), dict(current["aliases"][1]))
        aliases[1].pop(key)
        add_check("callsign_alias", f"{key} → {target}", {**current, "aliases": aliases})
    for phrase in sorted(short_garbage):
        add_check("short_garbage", phrase, current, garbage=short_garbage - {phrase})
    for phrase in active_blacklist:
        add_check("blacklist", phrase, current,
                  blacklist=tuple(item for item in active_blacklist if item != phrase))

    checks.sort(key=lambda item: (
        item["effect"] not in ("harmful", "mixed"),
        item["no_edits_delta"],
        -item["missed_delta"],
        -item["wer_delta"],
        item["kind"],
        item["label"],
    ))
    return {
        "records": active["records"],
        "current": {
            "no_edits": active["no_edits"],
            "missed": active["missed"],
            "wer": active["errors"] / max(1, active["words"]),
            "false_text": active["false_text"],
            "false_text_by_label": active["false_text_by_label"],
        },
        "non_speech_records": active["non_speech_records"],
        "checks": checks,
        "scope": "Текстові правила перевірено на збережених текстах мовлення та розміченому шумі, сигналах і кімнаті; Whisper, LLM і аудіофільтри не перезапускалися.",
    }


def compare(rows, dictionaries, short_garbage, *, before, after, show=12):
    """Порівняти два набори шаблонів на тих самих записах.

    ``before``/``after`` — словники аргументів для ``templates()``.
    """
    with templates(**before):
        base, base_out = run(rows, dictionaries, short_garbage)
    with templates(**after):
        new, new_out = run(rows, dictionaries, short_garbage)
    references = {r[0]: r[1] for r in rows}
    changed = []
    for record_id, (errors, out, _) in new_out.items():
        old_errors, old_out, _ = base_out[record_id]
        if out != old_out:
            changed.append({"id": record_id, "delta": errors - old_errors, "before": old_out, "after": out,
                            "reference": references[record_id]})
    changed.sort(key=lambda c: (c["delta"] > 0, -abs(c["delta"])))
    worse = [c for c in changed if c["delta"] > 0]
    better = [c for c in changed if c["delta"] < 0]
    examples = worse[:show] + better[:max(0, show - len(worse[:show]))]
    previous_patterns = set(before.get("patterns") or ())
    patterns = tuple(after.get("patterns") or ())
    added_patterns = [p for p in patterns if p not in previous_patterns]
    if added_patterns:
        rows_by_id = {row[0]: row for row in rows}
        for item in examples:
            row = rows_by_id[item["id"]]
            matched = []
            for index, pattern in enumerate(patterns):
                if pattern not in previous_patterns:
                    reduced = {**after, "patterns": patterns[:index] + patterns[index + 1:]}
                    with templates(**reduced):
                        _, without = run([row], dictionaries, short_garbage)
                    if without[item["id"]][1] != item["after"]:
                        matched.append(pattern)
            if matched:
                item["matched_patterns"] = matched
    return {"records": base["records"], "before": base, "after": new, "changed": len(changed),
            "better": len(better), "worse": len(worse), "same": len(changed) - len(better) - len(worse),
            # Спершу всі погіршення (їх треба бачити), потім найбільші поліпшення.
            "examples": examples}
