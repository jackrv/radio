"""Аналіз якості розпізнавання з командного рядка — ті самі методи, що й «Аналіз» у панелі.

Для людей і моделей: не треба щоразу перераховувати метрики самому чи входити в панель.
Архів і events.db лише читаються; приймач зупиняти не потрібно (усе на збережених текстах).

Приклади:
  .venv/bin/python -m lab.analysis summary              # головне: без правок, черги, по днях
  .venv/bin/python -m lab.analysis errors --days 7      # що заважає тексту без правок
  .venv/bin/python -m lab.analysis conditions           # без правок за тривалістю, гучністю, упевненістю
  .venv/bin/python -m lab.analysis callsigns            # точність по позивних і вигадані позивні
  .venv/bin/python -m lab.analysis records numbers -n 10  # самі фрази певного виду: еталон і що вийшло
  .venv/bin/python -m lab.analysis filters --profile pi-webcam  # що відкинув кожен фільтр і вердикт (docs/filters.md)
  .venv/bin/python -m lab.analysis checks               # можливі помилки у вашій розмітці (як «Потребує уваги»)
  .venv/bin/python -m lab.analysis runs                 # прогони лабораторії з «без правок»
  .venv/bin/python -m lab.analysis compare <A> <B>      # два прогони на спільних записах
  .venv/bin/python -m lab.analysis queue                # черга тестів лабораторії
Будь-яка команда з --json — сирі дані (як віддає API панелі).

Тексти містять справжні позивні й коди: показуйте їх лише локально, не переносьте в коміти.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import config
from shared import error_analysis
from shared.recordings import RecordingStore
from shared.paths import place

KINDS = tuple(error_analysis.CATEGORIES) + ("one_word", "edit_free", "worse", "better", "doubtful")


def _store():
    return RecordingStore(Path(config.RECORDINGS_DIR))


def _names():
    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        return error_analysis.callsign_names(db)
    finally:
        db.close()


def _pct(value):
    return "—" if value is None else f"{value * 100:.0f} %"


def _analyse(days, profile=None):
    return error_analysis.analyse(error_analysis.load_records(_store(), days, profile), _names())


def summary(args):
    store = _store()
    days = store.quality_by_day(60)
    counts = store.view_counts()
    import time
    fresh = store.view_counts(time.time() - 86400)
    effort = _analyse(0)["effort"]
    data = {"days": days, "effort": effort, "queues": {"radio_24h": fresh["radio"], "no_roles": counts["no_roles"],
            "false_text": counts["false_text"], "missed": counts["missed"], "unclear": counts["unclear"]},
            "confidence_low": store.confidence_threshold()}
    if args.json:
        return data
    total = sum(d["records"] for d in days)
    print(f"Розмічене мовлення за 60 днів: {total} фраз; без правок {effort['no_edits']}, ще з одним словом {effort['one_word']}, "
          f"у середньому {effort['errors_mean']:.1f} хибних слів на фразу." if total else "Розміченого мовлення немає.")
    print("\nДень         фраз  без правок  пропущено  WER Whisper → після  номери  адресат")
    for d in reversed(days):
        print(f"{d['day']}  {d['records']:>5}  {_pct(d['no_edits_share']):>10}  {_pct(d['missed_share']):>9}  "
              f"{_pct(d['wer_raw']):>11} → {_pct(d['wer_final']):<5}  {_pct(d['numbers']):>6}  {_pct(d['recipient']):>7}")
    q = data["queues"]
    print(f"\nЧерги розмітки: ефір без розмітки за 24 год {q['radio_24h']}, без ролей {q['no_roles']}, "
          f"хибний текст {q['false_text']}, пропущене {q['missed']}, нерозбірливі {q['unclear']}.")
    print(f"Межа «сумнівно» (найменш упевнена чверть): {data['confidence_low']}")


def errors(args):
    e = _analyse(args.days, args.profile)
    if args.json:
        return {k: v for k, v in e.items() if k not in ("conditions", "callsigns", "invented_only")}
    print(f"{e['with_edits']} з {e['records']} фраз потребували правок (одна фраза може мати кілька причин):")
    for key, c in sorted(e["categories"].items(), key=lambda kv: -kv[1]["count"]):
        if c["count"]:
            print(f"  {c['title']:<24} {c['count']:>5}   (records {key})")
    p = e["processing"]
    print(f"Обробка після Whisper: допомогла {p['better']}, зашкодила {p['worse']}.")
    print("Найчастіші заміни (почуто → правильно):")
    for s in e["substitutions"]:
        print(f"  {s['from']} → {s['to']}  ×{s['count']}  [{s['kind']}]")
    if e["number_confusions"]:
        print("Плутанина чисел (почуто → правильно):")
        for pair in e["number_confusions"]:
            print(f"  {pair['from']} → {pair['to']}  ×{pair['count']}")
    for title, key in (("Вигадані слова", "inserted"), ("Пропущені слова", "deleted")):
        if e[key]:
            print(f"{title}: " + ", ".join(f"{w['word']} ×{w['count']}" for w in e[key]))


def conditions(args):
    c = _analyse(args.days, args.profile)["conditions"]
    if args.json:
        return c
    for key, title in (("profile", "Канал запису"), ("duration", "Тривалість"), ("loudness", "Гучність"),
                       ("confidence", "Упевненість Whisper")):
        print(title + ":")
        for g in c[key]:
            extra = f"  ({g['range'][0]:.2f}…{g['range'][1]:.2f})" if g.get("range") else ""
            print(f"  {g['title']:<28} {g['records']:>4} фраз  без правок {_pct(g['share']):>5}  сер. {g['errors_mean']:.1f} хибних слів{extra}")
    if c["separation"] is not None:
        print(f"Різниця «без правок» між найбільш і найменш упевненою чвертю: {c['separation'] * 100:.0f} п.п. "
              f"({'упевненість передбачає правки' if c['separation'] >= 0.15 else 'слабо передбачає'}).")


VERDICTS = {"off": "ВИМКНУТИ: шкодить більше, ніж допомагає", "relax": "ПОСЛАБИТИ поріг",
            "keep": "лишити: працює", "label": "мало розмітки", "info": "втрачене мовлення"}


def filters(args):
    rows = error_analysis.filter_stats(_store(), args.days, args.profile)
    if args.json:
        return rows
    print(f"{'Фільтр':<26}{'відкинуто':>10}{'за добу':>9}{'шкода':>10}{'користь':>10}{'не розм.':>10}  Вердикт")
    for r in rows:
        verdict = VERDICTS[r["verdict"]]
        if r["verdict"] == "label":
            verdict += f": розмітьте ще {r['need']}"
        else:
            verdict += f" (шкода {_pct(r['lost_share'])})"
        if r["note"]:
            verdict += f" — {r['note']}"
        print(f"{r['title']:<26}{r['dropped']:>10}{r['per_day']:>9.1f}{r['lost']:>10}{r['caught']:>10}{r['unlabelled']:>10}  {verdict}")
    print("Що робити з кожним вердиктом і де вимикати — docs/filters.md.")


def callsigns(args):
    e = _analyse(args.days, args.profile)
    if args.json:
        return {"callsigns": e["callsigns"], "invented_only": e["invented_only"]}
    for c in e["callsigns"]:
        heard = ", ".join(f"{h['text'][:30]} ×{h['count']}" for h in c["heard"])
        print(f"  {c['name']:<14} вірно {c['correct']:>4} з {c['total']:<4} ({_pct(c['share'])})  пропущено {c['missing']:>3}  "
              f"вигадано {c['invented']:>3}  {('плутається: ' + heard) if heard else ''}")
    if e["invented_only"]:
        print("Вигадані, хоча в еталонах їх немає: " + ", ".join(f"{i['name']} ×{i['count']}" for i in e["invented_only"]))


def records(args):
    """Самі фрази певного виду — щоб подивитися приклади, а не лише лічильники."""
    store, names = _store(), _names()
    threshold = store.confidence_threshold() if args.kind == "doubtful" else None
    out = []
    for item in error_analysis.load_records(store, args.days, args.profile):
        found, _ = error_analysis.record_errors(item["reference"], item["final_text"], names)
        raw_errors = error_analysis.Levenshtein.distance(error_analysis.metric_words(item["reference"]), error_analysis.metric_words(item["raw_text"]))
        final_errors = error_analysis.Levenshtein.distance(error_analysis.metric_words(item["reference"]), error_analysis.metric_words(item["final_text"]))
        match = {"one_word": final_errors == 1, "edit_free": final_errors == 0, "worse": final_errors > raw_errors,
                 "better": final_errors < raw_errors,
                 "doubtful": threshold is not None and item["confidence"] is not None and item["confidence"] < threshold
                 }.get(args.kind, args.kind in found)
        if match:
            out.append({"id": item["id"], "reference": item["reference"], "final_text": item["final_text"], "raw_text": item["raw_text"],
                        "kinds": sorted(found), "word_errors": final_errors, "confidence": item["confidence"], "duration": item["duration"]})
        if len(out) >= args.limit:
            break
    if args.json:
        return out
    for r in out:
        print(f"{r['id'][:8]}  помилок {r['word_errors']}  [{', '.join(r['kinds']) or '—'}]")
        print(f"   еталон:  {r['reference']}")
        print(f"   вийшло:  {r['final_text']}")
        if r["raw_text"] != r["final_text"]:
            print(f"   Whisper: {r['raw_text']}")
    print(f"({len(out)} фраз; в архіві панелі: пошук «{','.join(r['id'][:8] for r in out[:20])}»)" if out else "Таких фраз немає.")


def checks(args):
    """Перепровірка вашої розмітки — те саме, що «Потребує уваги» в панелі (описки, ролі, подвоєння тощо)."""
    from collections import defaultdict
    from shared.suggestions import KINDS, reference_checks
    found = reference_checks(config.DB_PATH, _store())
    if args.json:
        return found
    groups = defaultdict(list)
    for item in found:
        groups[item["kind"]].append(item)
    for kind, items in groups.items():
        print(f"{KINDS[kind][0]} — {len(items)}")
        for item in items[:args.limit]:
            print(f"   {item['title']}")
    if not found:
        print("Зауважень до розмітки немає.")


def runs(args):
    from shared.experiments import list_runs
    items = list_runs(place(Path(config.BASE_DIR), ".local-experiments"), limit=args.limit)
    for run in items:
        s = run.get("summary") or {}
        run["no_edits"] = s.get("exact_phrases") / s["speech_records"] if s.get("speech_records") and s.get("exact_phrases") is not None else None
    if args.json:
        return items
    print("Прогін                                     записів  без правок  WER    номери  адресат  пропущено")
    for run in items:
        if not run["complete"] and not args.all:
            continue
        s = run.get("summary") or {}
        print(f"{run['name']:<42} {run['done']:>6}  {_pct(run['no_edits']):>10}  {_pct(s.get('wer')):>5}  {_pct(s.get('digits_exact')):>6}  "
              f"{_pct(s.get('recipient_accuracy_from_hypothesis')):>7}  {s.get('missed_speech', '—'):>9}{'' if run['complete'] else '  (не завершено)'}")


def compare(args):
    from shared.experiments import compare_runs
    r = compare_runs(place(Path(config.BASE_DIR), ".local-experiments"), args.a, args.b)
    if args.json:
        return r
    print(f"Спільних записів: {r['common']}")
    for key in ("exact_phrases", "wer", "digits_exact", "recipient_accuracy_from_hypothesis", "missed_speech", "false_text_on_non_speech"):
        print(f"  {key:<36} {r['a'].get(key)!s:>12}  {r['b'].get(key)!s:>12}")


def queue(args):
    from shared import lab_queue
    state = lab_queue.read(place(Path(config.BASE_DIR), ".local-experiments"))
    state["runner"]["active"] = lab_queue.runner_active(state)
    if args.json:
        return state
    print(f"Виконавець: {'працює — ' + str(state['runner'].get('state')) if state['runner']['active'] else 'не запущено'}")
    for job in state["jobs"]:
        print(f"  {job['status']:<10} {lab_queue.PRESETS.get(job['preset'], {}).get('title', job['preset'])}  {job.get('note', '')}")
    print("Рецепти: " + ", ".join(f"{k} — {p['title']}" for k, p in lab_queue.PRESETS.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="сирі дані JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary", parents=[common], help="без правок, черги розмітки, якість по днях")
    for name, helptext in (("errors", "що заважає тексту без правок"), ("conditions", "без правок за умовами запису"),
                           ("callsigns", "точність по позивних і вигадані позивні")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--days", type=int, default=0, help="лише останні N діб (0 — увесь архів)")
        p.add_argument("--profile", help="лише цей канал запису: mac-mic, pi-webcam… (docs/data.md)")
    p = sub.add_parser("filters", parents=[common], help="що відкинув кожен фільтр і чи він потрібен")
    p.add_argument("--days", type=int, default=0, help="лише останні N діб (0 — увесь архів)")
    p.add_argument("--profile", help="лише цей канал запису: mac-mic, pi-webcam…")
    p = sub.add_parser("records", parents=[common], help="фрази певного виду з еталоном і результатом")
    p.add_argument("kind", choices=KINDS)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--profile")
    p.add_argument("-n", "--limit", type=int, default=20)
    p = sub.add_parser("checks", parents=[common], help="можливі помилки у вашій розмітці")
    p.add_argument("-n", "--limit", type=int, default=10)
    p = sub.add_parser("runs", parents=[common], help="прогони лабораторії")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--all", action="store_true", help="разом із незавершеними")
    p = sub.add_parser("compare", parents=[common], help="два прогони на спільних записах")
    p.add_argument("a"); p.add_argument("b")
    sub.add_parser("queue", parents=[common], help="черга тестів лабораторії")
    args = parser.parse_args(argv)
    result = {"summary": summary, "errors": errors, "conditions": conditions, "callsigns": callsigns, "filters": filters, "records": records,
              "checks": checks, "runs": runs, "compare": compare, "queue": queue}[args.command](args)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
