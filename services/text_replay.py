"""Повтор текстової обробки на збережених виходах Whisper — без моделей і без зупинки приймача.

Для кожного розміченого запису мовлення береться текст, що надійшов у фінальний
аналіз (пропозиція Qwen, якщо її прийнято, інакше результат словесних правил або
сирий вихід Whisper), і проганяється через `core.text_analysis.analyze_text`
з чинними словниками. Варіанти змінюють лише шаблони, тож порівняння чесне:
ті самі записи, ті самі словники. Архів і events.db відкриваються лише для читання.
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from callsigns import normalize


def words(text):
    return re.findall(r"[^\W_]+", normalize(text or ""))


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def load_records(archive):
    """[(id, еталон, текст на вході аналізу)] для розміченого мовлення."""
    index = Path(archive) / "index.sqlite3"
    if not index.is_file():
        return []
    db = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = []
        for record_id, reference, data in db.execute(
                "SELECT id, reference, data FROM recordings WHERE reference IS NOT NULL AND label='speech'"):
            d = json.loads(data)
            text = d.get("llm_text") if d.get("llm_accepted") else d.get("before_llm")
            text = text or d.get("filtered_text") or d.get("raw_text") or ""
            if text.strip():
                rows.append((record_id, reference, text))
        return rows
    finally:
        db.close()


def load_setup(db_path):
    """(словники, короткий шум, чинні значення списків) з events.db лише для читання."""
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import load_active
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
    import callsigns as callsigns_module
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


def run(rows, dictionaries, short_garbage):
    """Метрики й вихід кожного запису за поточних шаблонів."""
    from core.text_analysis import analyze_text
    callsigns, commands, watch = dictionaries
    totals = {"records": 0, "words": 0, "errors": 0, "callsigns_ref": 0, "callsigns_hit": 0, "callsigns_false": 0,
              "commands_ref": 0, "commands_hit": 0, "commands_false": 0}
    outputs = {}
    for record_id, reference, text in rows:
        result = analyze_text(text, callsigns, commands, watch, short_garbage, Mock())
        out = "" if result.rejected else result.text
        errors = distance(words(out), words(reference))
        totals["records"] += 1
        totals["words"] += len(words(reference))
        totals["errors"] += errors
        outputs[record_id] = (errors, out)
        for kind, dictionary, found in (("callsigns", callsigns, result.callsigns), ("commands", commands, result.commands)):
            truth = {(n, num) for n, num, _ in dictionary.find(reference)}
            got = {(n, num) for n, num, _ in found}
            totals[f"{kind}_ref"] += len(truth)
            totals[f"{kind}_hit"] += len(truth & got)
            totals[f"{kind}_false"] += len(got - truth)
    totals["wer"] = totals["errors"] / max(1, totals["words"])
    return totals, outputs


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
    for record_id, (errors, out) in new_out.items():
        old_errors, old_out = base_out[record_id]
        if out != old_out:
            changed.append({"id": record_id, "delta": errors - old_errors, "before": old_out, "after": out,
                            "reference": references[record_id]})
    changed.sort(key=lambda c: (c["delta"] > 0, -abs(c["delta"])))
    worse = [c for c in changed if c["delta"] > 0]
    better = [c for c in changed if c["delta"] < 0]
    return {"records": base["records"], "before": base, "after": new, "changed": len(changed),
            "better": len(better), "worse": len(worse), "same": len(changed) - len(better) - len(worse),
            # Спершу всі погіршення (їх треба бачити), потім найбільші поліпшення.
            "examples": worse[:show] + better[:max(0, show - len(worse[:show]))]}
