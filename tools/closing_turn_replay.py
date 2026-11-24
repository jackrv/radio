"""Readonly: текстовий конвеєр із кінцевим правилом «прийнято, прийом» і без нього.

    .venv/bin/python -m tools.closing_turn_replay              # tune/train і tune/val
    .venv/bin/python -m tools.closing_turn_replay --holdout    # також holdout (лише для фінальної перевірки)
    .venv/bin/python -m tools.closing_turn_replay --show       # кінці фраз, що погіршилися

Поділ: holdout — lab.asr_lab.split_of (20-хв блоки); tune → train/val за іншою сіллю тих самих блоків.
Архів і events.db лише читаються. Без моделей, приймач не зупиняти.
"""
import sys, hashlib, sqlite3
from pathlib import Path
sys.path.insert(0, ".")
import config
import shared.template_text as tt
from shared.text_replay import load_records, load_setup, run, templates
from lab.asr_lab import split_of, SPLIT_BLOCK_S
from shared.text_metrics import metric_words
arch = str(config.RECORDINGS_DIR)
rows = load_records(arch, include_non_speech=True)
dicts, short_garbage, values = load_setup(config.DB_PATH, arch)
db = sqlite3.connect(f"file:{arch}/index.sqlite3?mode=ro", uri=True)
created = dict(db.execute("SELECT id, created FROM recordings WHERE reference IS NOT NULL")); db.close()
def part(rid):
    c = created[rid]; s = split_of(c)
    if s == "holdout": return "holdout"
    return "val" if int(hashlib.sha256(f"pp-val:{int(c // SPLIT_BLOCK_S)}".encode()).hexdigest(), 16) % 4 == 0 else "train"
current = {"phrases": values.get("phrase_templates", ()), "patterns": values.get("phrase_patterns", ()),
           "aliases": (values.get("aliases", {}), values.get("callsign_aliases", {}))}
show = "--show" in sys.argv
for name in ("train", "val", "holdout") if "--holdout" in sys.argv else ("train", "val"):
    sub = [r for r in rows if part(r[0]) == name]
    res = {}
    for on in (False, True):
        tt.CLOSING_TURN = on
        with templates(**current):
            res[on] = run(sub, dicts, short_garbage)
    (a, oa), (b, ob) = res[False], res[True]
    better = sum(1 for k in oa if oa[k][2] == "speech" and ob[k][0] < oa[k][0])
    worse = [k for k in oa if oa[k][2] == "speech" and ob[k][0] > oa[k][0]]
    neg = sum(1 for k in oa if oa[k][2] != "speech" and ob[k][1] != oa[k][1])
    print(f"{name}: фраз {a['records']}, без правок {a['no_edits']}→{b['no_edits']}, WER {100*a['wer']:.2f}→{100*b['wer']:.2f}%, "
          f"краще {better}, гірше {len(worse)}, пропущено {a['missed']}→{b['missed']}, змін на немовленні {neg}, "
          f"хибний текст {a['false_text']}→{b['false_text']}, позивні {a['callsigns_hit']}→{b['callsigns_hit']} (хибні {a['callsigns_false']}→{b['callsigns_false']}), "
          f"команди {a['commands_hit']}→{b['commands_hit']} (хибні {a['commands_false']}→{b['commands_false']})")
    if show:
        ref = {r[0]: r[1] for r in sub}
        for k in worse: print("   ГІРШЕ: …", " ".join(metric_words(ref[k])[-4:]), "| було …", " ".join(metric_words(oa[k][1])[-4:]), "| стало …", " ".join(metric_words(ob[k][1])[-4:]))
