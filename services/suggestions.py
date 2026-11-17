"""Пропозиції з розмітки оператора й автоматичні нагадування про них.

Періодично (у процесі локальної панелі, не приймача) збирається те, що накопичилося
в усіх списках: кандидати в алиаси позивних і в короткий шум, заміни слів, що чекають
дозволу, конфлікти навчання, номери позивних з еталонів, яких немає у словнику,
вимкнені шаблони фраз, які на еталонах лише покращують текст. Усе це видно в
«Огляд → Потребує уваги»; нові (за останню добу) позначені окремо.

Нічого не застосовується автоматично: оператор вирішує в панелі.
"""
import re
import sqlite3
import threading
import time
from collections import Counter

from services.sqlite_connection import get_db

CHECK_MINUTES = 30
DEEP_CHECK_HOURS = 3           # шаблони фраз вимагають повтору обробки на архіві
NEW_HOURS = 24                 # «нова» пропозиція — що з’явилася за останню добу
MIN_REPEATS = 2                # кандидат — те, що повторилося хоча б двічі
MIN_ALIAS_SIMILARITY = 50      # слабко схоже звичайне слово (≈40 %) як алиас не пропонуємо

KINDS = {
    "alias": ("Алиаси позивних", "texts:callsign_aliases"),
    "noise": ("Короткий шум і галюцинації", "texts:short_garbage"),
    "approval": ("Заміни чекають дозволу", "learning"),
    "conflict": ("Конфлікти навчання", "learning"),
    "dictionary": ("Номери й позивні для словника", "dictionaries"),
    "pattern": ("Шаблони фраз, що покращують текст", "texts:phrase_patterns"),
    "ref_text": ("Можливі помилки в еталонах", "recordings"),
    "ref_label": ("Тип запису суперечить тексту", "recordings"),
    "ref_roles": ("Ролі не збігаються з текстом", "recordings"),
    "ref_event": ("Подія й Telegram не оновлені за еталоном", "recordings"),
    "dict_overlap": ("Один номер у кількох позивних", "dictionaries"),
    "list_conflict": ("Рядки списків суперечать словникам", "texts"),
}
REFERENCE_CHECK_DAYS = 14       # перепровірка тексту еталонів — за останні два тижні розмітки
NON_SPEECH = ("noise", "room", "notification")
LABEL_NAMES = {"noise": "шум ефіру", "room": "кімната", "notification": "сигнал"}


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS suggestion_state (
        key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        first_seen REAL NOT NULL,
        record TEXT)""")
    if "record" not in {row[1] for row in db.execute("PRAGMA table_info(suggestion_state)")}:
        db.execute("ALTER TABLE suggestion_state ADD COLUMN record TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS suggestion_meta (id INTEGER PRIMARY KEY CHECK(id = 1), "
               "last_deep REAL, deep_json TEXT)")


def _suggestion(kind, key, title, count=1, record=None):
    return {"kind": kind, "key": f"{kind}:{key}", "title": title, "count": count, "record": record}


def _words(text):
    from callsigns import normalize
    return re.findall(r"[^\W_]+", normalize(text or ""))


def reference_checks(db_path, store, *, now=None):
    """Перепровірка розмітки: можливі помилки в еталонах і суперечності з подіями та ролями."""
    from services import daily_codes
    from services.telegram_media_store import is_hidden
    from services.text_check import check_with_db
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import sync_aliases
    now = time.time() if now is None else now
    out = []
    with get_db(db_path) as db:
        sync_aliases(db)
        callsigns, _, _ = dictionaries_from_lines(read_dictionary_lines(db))
        for r in store.reviewed_records():
            rid, label, reference = r["id"], r["label"], (r["reference"] or "").strip()
            short = rid[:8]
            speech = label in ("speech", "unclear")
            if speech and not reference:
                out.append(_suggestion("ref_label", rid, f"{short}: позначено мовлення, але тексту немає", record=rid))
            elif label in NON_SPEECH and reference:
                out.append(_suggestion("ref_label", rid, f"{short}: «{LABEL_NAMES[label]}», але є текст «{reference[:40]}»", record=rid))
            recipient, sender = (r["reference_recipient"] or "").strip(), (r["reference_sender"] or "").strip()
            text_words = " " + " ".join(_words(reference)) + " "
            role_issue = None
            if recipient and sender and _words(recipient) == _words(sender):
                role_issue = f"«кому» й «від» однакові ({recipient})"
            elif r["multiple_messages"] and (recipient or sender):
                role_issue = "кілька передач, але вказано ролі"
            else:
                # Через словник: «четвертий» = дуб 4, алиаси й форми звертання враховано.
                heard = {(n, num) for n, num, _ in callsigns.find(reference)}
                heard_names = {n for n, _ in heard}
                for title, value in (("кому", recipient), ("від", sender)):
                    if not value:
                        continue
                    wanted = {(n, num) for n, num, _ in callsigns.find(value)}
                    if wanted:
                        missing = [w for w in wanted if w not in heard and not (w[1] is None and w[0] in heard_names)]
                    else:
                        missing = [value] if f" {' '.join(_words(value))} " not in text_words else []
                    if missing:
                        role_issue = f"«{title}: {value}» не згадано в тексті"
                        break
            if speech and reference and role_issue:
                out.append(_suggestion("ref_roles", rid, f"{short}: {role_issue}", record=rid))
            fix = r["fix_id"] or ""
            if speech and reference and re.fullmatch(r"db:[1-9][0-9]{0,17}", fix):
                event = db.execute("SELECT text FROM radio_events WHERE id=?", (int(fix[3:]),)).fetchone()
                if event and _words(event[0]) != _words(reference) and not is_hidden(db_path, fix, db=db):
                    out.append(_suggestion("ref_event", rid, f"{short}: подія «{event[0][:40]}»", record=rid))
            if speech and reference and (r["reviewed_at"] or 0) >= now - REFERENCE_CHECK_DAYS * 86400:
                codes = daily_codes.read(db, daily_codes.local_day(r["created"]))
                issues = [i for i in check_with_db(db, reference, codes)["issues"] if i["kind"] != "format"]
                if issues:
                    more = f" (+{len(issues) - 1})" if len(issues) > 1 else ""
                    out.append(_suggestion("ref_text", rid, f"{short}: {issues[0]['message']}{more}", record=rid))
    return out


def dictionary_conflicts(db_path):
    """Номери, дозволені кільком позивним, і рядки списків, що суперечать словникам."""
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import LIST_TYPES, check_against_dictionaries, ensure_tables, load_active
    out = []
    with get_db(db_path) as db:
        callsigns, _, _ = dictionaries_from_lines(read_dictionary_lines(db))
        owners = {}
        for name in callsigns.names:
            for number in callsigns.numbers.get(name) or ():
                owners.setdefault(number, []).append(name)
        for number, names in sorted(owners.items()):
            if len(names) > 1:
                out.append(_suggestion("dict_overlap", number, f"номер {number} є в позивних {', '.join(sorted(names))}"))
        ensure_tables(db)
        _, active = load_active(db)
        for list_type in LIST_TYPES:
            errors, _ = check_against_dictionaries(db, list_type, active.get(list_type) or ([] if list_type not in ("aliases", "callsign_aliases") else {}))
            for error in errors:
                out.append(_suggestion("list_conflict", f"{list_type}:{error}", error))
    return out


def collect(db_path, store, *, patterns=None):
    """Поточні пропозиції. ``patterns`` — готовий результат глибокої перевірки шаблонів (або None)."""
    from callsigns import Callsigns, normalize
    from services.alias_candidates import collect as alias_collect
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.miniapp_management import learning_queue
    from services.noise_candidates import collect as noise_collect, key as noise_key
    from services.text_lists import _dictionary_names, ensure_tables, load_active, sync_aliases

    out = []
    with get_db(db_path) as db:
        ensure_tables(db)
        sync_aliases(db)
        _, active = load_active(db)
        callsigns, commands, watch = dictionaries_from_lines(read_dictionary_lines(db))
        names, _ = _dictionary_names(db)
    records = store.labelled_texts()
    speech = [r for r in records if r["label"] in ("speech", "unclear")]

    for c in alias_collect([{"id": "", "reference": r["reference"], "raw_text": r["raw_text"]} for r in speech],
                           callsigns, names, active.get("callsign_aliases", {})):
        # Лише чисті кандидати: без позначок (інший словник, позивний без номера…) і помітно схожі.
        if c["addable"] and not c["flags"] and c["similarity"] >= MIN_ALIAS_SIMILARITY and c["count"] >= MIN_REPEATS:
            out.append(_suggestion("alias", f"{c['callsign']}:{c['heard']}", f"«{c['heard']}» → {c['callsign']} (×{c['count']})", c["count"]))

    noise_keys = {noise_key(v) for v in (*active.get("blacklist", ()), *active.get("short_garbage", ()))}
    for c in noise_collect(records, noise_keys, set(names)):
        if c["addable"] and c["count"] >= MIN_REPEATS and c["reached_text"]:
            out.append(_suggestion("noise", f"{c['list']}:{c['text']}", f"«{c['text']}» (×{c['count']})", c["count"]))

    queue = learning_queue(db_path)
    for item in queue["items"]:
        if item["state"] == "needs_approval":
            out.append(_suggestion("approval", f"{item['original']}>{item['corrected']}", f"«{item['original']}» → «{item['corrected']}»"))
        elif item["state"] == "conflict":
            out.append(_suggestion("conflict", item["original"], f"«{item['original']}»"))

    # Номери відомих позивних і нові позивні з еталонів, що повторилися.
    gaps = Counter()
    for r in speech:
        words = re.findall(r"[^\W_]+", normalize(r["reference"] or ""))
        for i in range(len(words) - 1):
            word, number = words[i], words[i + 1]
            if not (word.isalpha() and number.isdigit()) or len(word) < 3:
                continue
            if word in callsigns.names:
                allowed = callsigns.numbers.get(word) or set()
                if allowed and number not in allowed and word not in callsigns.any_number:
                    gaps[(word, number)] += 1
    for (word, number), count in gaps.items():
        if count >= MIN_REPEATS:
            out.append(_suggestion("dictionary", f"{word}:{number}", f"{word} {number} (×{count} в еталонах)", count))

    for line in patterns or ():
        out.append(_suggestion("pattern", line["pattern"], f"{line['pattern']} (краще {line['better']}, гірше 0)", line["better"]))
    out += reference_checks(db_path, store)
    out += dictionary_conflicts(db_path)
    return out


def deep_patterns(db_path, store):
    """Вимкнені шаблони фраз, кожен з яких окремо лише покращує текст на еталонах."""
    from services.text_lists import read_lists
    from services.text_replay import compare, load_records, load_setup
    rows = load_records(store.root)
    if not rows:
        return []
    dictionaries, short_garbage, values = load_setup(db_path)
    current = {"phrases": values.get("phrase_templates", ()), "patterns": values.get("phrase_patterns", ()),
               "aliases": (values.get("aliases", {}), values.get("callsign_aliases", {}))}
    disabled = [e["raw_line"] for e in read_lists(db_path)["lists"]["phrase_patterns"]["entries"] if not e["enabled"]]
    good = []
    for line in disabled:
        try:
            result = compare(rows, dictionaries, short_garbage, before=current,
                             after={**current, "patterns": (*current["patterns"], line)}, show=0)
        except Exception:
            continue
        if result["better"] and not result["worse"] and result["after"]["callsigns_false"] <= result["before"]["callsigns_false"]:
            good.append({"pattern": line, "better": result["better"]})
    return good


def refresh(db_path, store, *, deep=False, now=None):
    """Оновити стан пропозицій. Повертає (пропозиції, ключі нових)."""
    import json
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        ensure_table(db)
        meta = db.execute("SELECT last_deep, deep_json FROM suggestion_meta WHERE id=1").fetchone()
    last_deep, deep_json = meta if meta else (None, None)
    patterns = json.loads(deep_json) if deep_json else []
    if deep or last_deep is None or now - last_deep >= DEEP_CHECK_HOURS * 3600:
        patterns = deep_patterns(db_path, store)
        last_deep = now
    current = collect(db_path, store, patterns=patterns)
    keys = {s["key"]: s for s in current}
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        known = {row[0] for row in db.execute("SELECT key FROM suggestion_state")}
        # Застосовані чи зниклі пропозиції прибираються; нові — запам’ятовуються з часом появи.
        db.executemany("DELETE FROM suggestion_state WHERE key=?", [(k,) for k in known if k not in keys])
        fresh = [s for k, s in keys.items() if k not in known]
        db.executemany("INSERT INTO suggestion_state (key, kind, title, first_seen, record) VALUES (?,?,?,?,?)",
                       [(s["key"], s["kind"], s["title"], now, s.get("record")) for s in fresh])
        # Назва могла змінитися (лічильник «×N»): оновлюємо.
        db.executemany("UPDATE suggestion_state SET title=? WHERE key=?", [(s["title"], k) for k, s in keys.items()])
        db.execute("INSERT INTO suggestion_meta (id, last_deep, deep_json) VALUES (1,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET last_deep=excluded.last_deep, deep_json=excluded.deep_json",
                   (last_deep, json.dumps(patterns, ensure_ascii=False)))
    return current, [s["key"] for s in fresh]


def overview(db_path, now=None):
    """Що показати в «Потребує уваги»: групи з кількістю, скільки з них нові й приклади."""
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        ensure_table(db)
        rows = db.execute("SELECT kind, title, first_seen, record FROM suggestion_state ORDER BY first_seen DESC").fetchall()
        meta = db.execute("SELECT last_deep FROM suggestion_meta WHERE id=1").fetchone()
    groups = {}
    for kind, title, first_seen, record in rows:
        group = groups.setdefault(kind, {"kind": kind, "title": KINDS.get(kind, (kind, ""))[0],
                                         "target": KINDS.get(kind, ("", "overview"))[1], "count": 0, "new": 0,
                                         "examples": [], "records": []})
        if record and len(group["records"]) < 20:
            group["records"].append(record[:8])
        group["count"] += 1
        group["new"] += first_seen >= now - NEW_HOURS * 3600
        if len(group["examples"]) < 3:
            group["examples"].append(title)
    for group in groups.values():
        # Записи архіву відкриваються пошуком за номерами (до 20 за раз).
        if group["records"]:
            group["target"] = "recordings:" + ",".join(group["records"])
    return {"groups": [groups[k] for k in KINDS if k in groups], "checked": meta is not None}


class SuggestionWorker:
    """Фонова перевірка в процесі панелі: раз на CHECK_MINUTES, без впливу на приймач."""

    def __init__(self, db_path, store, log, *, interval=CHECK_MINUTES * 60):
        self.db_path, self.store, self.log, self.interval = db_path, store, log, interval
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="suggestions", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        # Невелика затримка: панель і приймач спершу запускаються.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                refresh(self.db_path, self.store)
            except (sqlite3.Error, OSError, ValueError) as exc:
                self.log.warning("Пропозиції з розмітки не оновлено: %s", type(exc).__name__)
            except Exception:
                self.log.exception("Помилка перевірки пропозицій з розмітки")
            if self._stop.wait(self.interval):
                return
