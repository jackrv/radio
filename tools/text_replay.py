"""Повтор текстової обробки на збережених виходах Whisper з архіву — без моделей і без зупинки приймача.

    .venv/bin/python -m tools.text_replay                         # чинні шаблони проти стану до 17.11
    .venv/bin/python -m tools.text_replay --patterns FILE         # додати шаблони фраз із файла (рядок — шаблон)
    .venv/bin/python -m tools.text_replay --patterns FILE --each  # внесок кожного шаблону окремо

Лише читання: архів і events.db відкриваються в режимі mode=ro.
"""
import argparse
from pathlib import Path

from services.text_replay import compare, load_records, load_setup

ROOT = Path(__file__).resolve().parents[1]
OLD_PHRASES = ("прийом", "прийнято", "прийняв", "посилку отримав", "на звʼязку", "на звʼязок",
               "перевірка звʼязку", "до роботи приступив", "роботу закінчив", "роботу розпочав",
               "чую та розбираю", "чую і розбираю", "повторіть голос не пройшов")


def describe(name, result):
    b, a = result["before"], result["after"]
    print(f"\n== {name}: записів {result['records']}, WER {b['wer']:.3f} → {a['wer']:.3f}, "
          f"позивні {b['callsigns_hit']} → {a['callsigns_hit']}/{a['callsigns_ref']} "
          f"(хибних {b['callsigns_false']} → {a['callsigns_false']}), "
          f"команди {b['commands_hit']} → {a['commands_hit']}/{a['commands_ref']}, "
          f"змінено фраз {result['changed']}: краще {result['better']}, гірше {result['worse']}")


def show(result):
    for c in result["examples"]:
        print(f"   {c['delta']:+d} | {c['id'][:8]} | було:  {c['before'][:80]!r}\n"
              f"        | стало: {c['after'][:80]!r}\n        | еталон: {c['reference'][:80]!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", default=str(ROOT / ".local-recordings"))
    parser.add_argument("--db", default=str(ROOT / "events.db"))
    parser.add_argument("--patterns", help="файл із шаблонами фраз для перевірки")
    parser.add_argument("--each", action="store_true", help="перевірити кожен шаблон окремо")
    parser.add_argument("--show", type=int, default=8, help="скільки змінених фраз показати")
    args = parser.parse_args()
    rows = load_records(args.archive)
    dictionaries, short_garbage, values = load_setup(args.db)
    current = {"phrases": values.get("phrase_templates", ()), "patterns": values.get("phrase_patterns", ()),
               "aliases": (values.get("aliases", {}), values.get("callsign_aliases", {}))}
    if not args.patterns:
        import core.template_text as tt
        saved = tt.join_split_callsign, tt.trim_loops

        def old_rules(on):
            tt.join_split_callsign, tt.trim_loops = ((lambda text, _c: text), (lambda text: text)) if on else saved
        old_rules(True)
        try:
            from services.text_replay import run, templates
            with templates(**{**current, "phrases": OLD_PHRASES, "patterns": ()}):
                before, _ = run(rows, dictionaries, short_garbage)
        finally:
            old_rules(False)
        result = compare(rows, dictionaries, short_garbage, before={**current, "phrases": OLD_PHRASES, "patterns": ()},
                         after=current, show=args.show)
        result["before"] = before
        describe("до 17.11 → зараз (без нових правил «на 109» і зациклень у «до»)", result)
        show(result)
        return
    lines = [line.strip() for line in Path(args.patterns).read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    for chosen in ([[line] for line in lines] if args.each else [lines]):
        result = compare(rows, dictionaries, short_garbage, before=current,
                         after={**current, "patterns": (*current["patterns"], *chosen)}, show=args.show)
        describe(chosen[0] if args.each else f"{len(chosen)} шаблонів", result)
        if not args.each:
            show(result)


if __name__ == "__main__":
    main()
