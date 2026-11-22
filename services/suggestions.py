"""Пропозиції з розмітки оператора й автоматичні нагадування про них.

Періодично (у процесі локальної панелі, не приймача) збирається те, що накопичилося
в усіх списках: кандидати в аліаси позивних і в короткий шум, заміни слів, що чекають
дозволу, конфлікти навчання, номери позивних з еталонів, яких немає у словнику,
вимкнені шаблони фраз, які на еталонах лише покращують текст. Усе це видно в
«Огляд → Потребує уваги»; нові (за останню добу) позначені окремо.

Нічого не застосовується автоматично: оператор вирішує в панелі. Після будь-якої зміни
в панелі перевірка повторюється за кілька секунд, тож виконане зникає саме. Те, що
оператор вирішив лишити як є, прибирається кнопкою «Готово» (без підтверджень) і
не повертається, доки не з’явиться нова пропозиція.
"""
import re
import json
import sqlite3
import threading
import time
from collections import Counter

from services.sqlite_connection import get_db

CHECK_MINUTES = 30
POKE_DELAY_S = 5               # після правки в панелі — коротка пауза, щоб зібрати кілька змін разом
DEEP_CHECK_HOURS = 3           # шаблони фраз вимагають повтору обробки на архіві
NEW_HOURS = 24                 # «нова» пропозиція — що з’явилася за останню добу
MIN_REPEATS = 2                # кандидат — те, що повторилося хоча б двічі
MIN_ALIAS_SIMILARITY = 50      # слабко схоже звичайне слово (≈40 %) як аліас не пропонуємо

# Вид: (назва, куди веде «Переглянути», пояснення під рядком).
KINDS = {
    "alias": ("Аліаси позивних", "texts:callsign_aliases",
              "Так Whisper кілька разів почув позивний. Перевірено на ваших еталонах: з цим аліасом програма знаходить більше "
              "позивних і не додає хибних. Перегляньте чернетку рядка в редакторі й збережіть, якщо згодні."),
    "noise": ("Короткий шум і галюцинації", "texts:short_garbage",
              "Цей текст повторюється на записах, які ви позначили як шум. Перевірте чернетку в редакторі й збережіть, якщо згодні."),
    "approval": ("Заміни чекають дозволу", "learning",
                 "Ви кілька разів однаково виправили звичайне слово. Програма не вмикає таку заміну сама: дозвольте її в «Навчанні», якщо вона завжди правильна, або вимкніть."),
    "conflict": ("Конфлікти навчання", "learning", ""),
    "dictionary": ("Номери позивних для словника", "dictionaries",
                   "Ці номери кілька разів є у ваших еталонах, але не у словнику. Перевірте чернетку в редакторі або натисніть «Готово»."),
    "pattern": ("Шаблони фраз, що покращують текст", "texts:phrase_patterns",
                "Вимкнений шаблон на ваших еталонах лише покращує текст. Перевірте тимчасове ввімкнення й збережіть або натисніть «Готово»."),
    "ref_text": ("Можливі описки в еталонах", "recordings",
                 "Слово в еталоні схоже на назву зі словника, але написане інакше."),
    "ref_label": ("Тип запису суперечить тексту", "recordings",
                  "«Мовлення» без тексту: впишіть текст або, якщо слів не розібрати, змініть тип на шум чи кімнату."),
    "ref_roles": ("Ролі не збігаються з текстом", "recordings",
                  "Для запису з кількома передачами ролі «кому/від» не заповнюють — очистіть їх."),
    "ref_event": ("Подія не оновлена за еталоном", "recordings",
                  "Ці записи ви виправили в архіві раніше, ніж з’явилося автоматичне оновлення подій. Журнал подій "
                  "і повідомлення в Telegram ще з текстом Whisper. «Оновити всі» замінить їхній текст вашим еталоном "
                  "(підпис повідомлення в Telegram зміниться в усіх, кому воно надійшло; правка піде в навчання, як "
                  "після редагування в Mini App). Аудіо й ролі не змінюються."),
    "ref_repeat": ("Подвоєні службові слова в еталонах", "recordings",
                   "Службове слово двічі поспіль — найчастіше описка («прийом, прийом» замість «прийнято, прийом»). "
                   "Прослухайте й виправте або «Готово», якщо так і казали."),
    "ref_self": ("«Від» суперечить «я …»", "recordings",
                 "У тексті «я Х», а в ролі «від» — інший позивний або «не вказано у фразі»."),
    "ref_format": ("Еталон не за правилами оформлення", "recordings",
                   "Чотири цифри разом — пишіть парами, як вимовляють («48 62»)."),
    "ref_unchanged": ("Текст Whisper збережено без змін", "recordings",
                      "Whisper був найменш упевнений, а еталон збігається з його текстом слово в слово. Серед таких фраз "
                      "без правок майже не буває — можливо, запис збережено, не прослухавши."),
    "list_conflict": ("Рядки списків суперечать словникам", "texts", ""),
}
# Службові слова, яких двічі поспіль не кажуть (позивний двічі — «Клен, Клен» — за протоколом, це не описка).
NO_REPEAT = {"прийом", "прийнято", "прийняв", "прийняла", "я", "на", "до", "та", "і", "й", "в", "у", "з", "як", "від", "за", "по", "не"}
_FOUR_DIGITS = re.compile(r"(?<!\d)\d{4}(?!\d)")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d\s+(?:році|рік|року|роком)", re.IGNORECASE)
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


def ensure_dismissed(db):
    db.execute("""CREATE TABLE IF NOT EXISTS suggestion_dismissed (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, dismissed_at REAL NOT NULL)""")
    columns = {row[1] for row in db.execute('PRAGMA table_info(suggestion_dismissed)')}
    for name, definition in [('title', "TEXT NOT NULL DEFAULT ''"), ('record', 'TEXT')]:
        if name not in columns: db.execute(f'ALTER TABLE suggestion_dismissed ADD COLUMN {name} {definition}')


def ensure_muted(db):
    db.execute("CREATE TABLE IF NOT EXISTS suggestion_muted (kind TEXT PRIMARY KEY, muted_at REAL NOT NULL)")


def muted_kinds(db_path):
    """Види, які оператор вимкнув: їх не перевіряють і не показують у «Потребує уваги»."""
    with get_db(db_path) as db:
        ensure_muted(db)
        return {row[0] for row in db.execute("SELECT kind FROM suggestion_muted")}


def set_muted(db_path, kind, muted, *, now=None):
    if kind not in KINDS:
        raise ValueError("Невідомий вид пропозицій")
    with get_db(db_path) as db:
        ensure_muted(db)
        if muted:
            db.execute("INSERT OR REPLACE INTO suggestion_muted (kind, muted_at) VALUES (?, ?)",
                       (kind, time.time() if now is None else now))
            # Поточні пропозиції вимкненого виду зникають одразу, не чекаючи перевірки.
            ensure_table(db)
            db.execute("DELETE FROM suggestion_state WHERE kind=?", (kind,))
        else:
            db.execute("DELETE FROM suggestion_muted WHERE kind=?", (kind,))
    return {"kind": kind, "muted": bool(muted)}


def dismiss(db_path, kind, keys=None, *, now=None):
    """«Готово»: прибрати поточні пропозиції виду (або лише ``keys``). Повертає кількість."""
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        ensure_table(db)
        ensure_dismissed(db)
        rows = db.execute("SELECT key,title,record FROM suggestion_state WHERE kind=?", (kind,)).fetchall()
        chosen = [(k,title,record) for k,title,record in rows if keys is None or k in set(keys)]
        db.executemany("INSERT OR REPLACE INTO suggestion_dismissed (key, kind, dismissed_at,title,record) VALUES (?,?,?,?,?)",
                       [(k, kind, now, title, record) for k,title,record in chosen])
    return len(chosen)


def restore(db_path, kind=None, *, keys=None):
    """Повернути приховані пропозиції (усі або одного виду)."""
    with get_db(db_path) as db:
        ensure_dismissed(db)
        if keys is not None:
            cursor = db.execute("DELETE FROM suggestion_dismissed WHERE key IN (SELECT value FROM json_each(?))", (json.dumps(keys),))
        else:
            cursor = db.execute("DELETE FROM suggestion_dismissed WHERE ?1 IS NULL OR kind=?1", (kind,))
        return cursor.rowcount


def decisions(db_path, offset=0, limit=100):
    """Рішення «Готово» зберігаються, навіть коли пропозиція тимчасово зникла."""
    with get_db(db_path) as db:
        ensure_dismissed(db)
        rows = db.execute('SELECT key,kind,dismissed_at,title,record FROM suggestion_dismissed ORDER BY dismissed_at DESC LIMIT ? OFFSET ?',
                          (limit,offset)).fetchall()
        total = db.execute('SELECT COUNT(*) FROM suggestion_dismissed').fetchone()[0]
    return {'items': [dict(zip(('key','kind','at','title','record'), r)) for r in rows], 'total': total}


def _suggestion(kind, key, title, count=1, record=None):
    return {"kind": kind, "key": f"{kind}:{key}", "title": title, "count": count, "record": record}


def _words(text):
    from services.text_lists import blacklist_key
    return blacklist_key(text or "").split()


ALIAS_CHECK_MAX = 10   # кожен кандидат — повтор обробки (~1 с), тому не більше стількох за перевірку


def _alias_effects(db_path, store, candidates, *, progress=None):
    """Кандидати в аліаси, які на ваших еталонах справді щось дають, і що саме.

    Повтор текстової обробки зі списком «+ цей аліас» проти чинного: лишаються ті, з якими знайдено
    більше позивних або фрази кращі, і при цьому не більше хибних позивних і жодна фраза не гірша.
    Решта (позивний і так знаходиться) — не пропонуються.
    """
    if not candidates:
        return []
    from services.text_replay import compare, load_records, load_setup
    rows = load_records(store.root)
    if not rows:
        return []
    dictionaries, short_garbage, values = load_setup(db_path)
    current = {"phrases": values.get("phrase_templates", ()), "patterns": values.get("phrase_patterns", ()),
               "aliases": (values.get("aliases", {}), values.get("callsign_aliases", {}))}
    useful = []
    for c in candidates:
        aliases = dict(current["aliases"][1])
        aliases[c["heard"]] = c["callsign"]
        try:
            r = compare(rows, dictionaries, short_garbage, before=current,
                        after={**current, "aliases": (current["aliases"][0], aliases)}, show=0, progress=progress)
        except Exception:
            continue
        found = r["after"]["callsigns_hit"] - r["before"]["callsigns_hit"]
        if r["worse"] or r["after"]["callsigns_false"] > r["before"]["callsigns_false"] or not (found > 0 or r["better"]):
            continue
        parts = ([f"+{found} знайдених позивних"] if found > 0 else []) + ([f"краще фраз: {r['better']}"] if r["better"] else [])
        useful.append((c, ", ".join(parts)))
    return useful


def reference_checks(db_path, store, *, now=None):
    """Перепровірка розмітки: можливі помилки в еталонах і суперечності з подіями та ролями."""
    from services.telegram_media_store import is_hidden
    from services.text_check import check_with_db
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import sync_aliases
    from core.analyzer import _SELF, fmt, split_roles
    from core.text_metrics import metric_words
    from services.error_analysis import load_records
    now = time.time() if now is None else now
    out = []
    speech_data = {item["id"]: item for item in load_records(store)}   # текст Whisper, тривалість, упевненість
    threshold = store.confidence_threshold()
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
                # Через словник: «четвертий» = дуб 4, аліаси й форми звертання враховано.
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
            words = metric_words(reference)
            if speech and reference:
                repeated = next((w for a, w in zip(words, words[1:]) if a == w and w in NO_REPEAT), None)
                if repeated:
                    hint = " — мабуть, «прийнято, прийом»" if repeated == "прийом" else ""
                    out.append(_suggestion("ref_repeat", rid, f"{short}: «{repeated}, {repeated}»{hint}", record=rid))
                if _FOUR_DIGITS.search(_YEAR.sub("", reference)):
                    out.append(_suggestion("ref_format", rid, f"{short}: «{_FOUR_DIGITS.search(_YEAR.sub('', reference)).group(0)}» — парами", record=rid))
            # «я Х» — Х відправник; перевіряємо лише заповнену роль (None — ролі ще не розмічено).
            if speech and reference and not r["multiple_messages"] and r["reference_sender"] is not None and _SELF.search(reference):
                _, said = split_roles(reference, callsigns)
                if len(said) == 1 and metric_words(r["reference_sender"]) != metric_words(fmt(said)):
                    shown = r["reference_sender"] or "не вказано у фразі"
                    out.append(_suggestion("ref_self", rid, f"{short}: «я {fmt(said)}», а «від: {shown}»", record=rid))
            data = speech_data.get(rid)
            if label == "speech" and data and reference:
                if threshold is not None and data["confidence"] is not None and data["confidence"] < threshold \
                        and words == metric_words(data["raw_text"]):
                    out.append(_suggestion("ref_unchanged", rid, f"{short}: «{reference[:40]}»", record=rid))
            if speech and reference and (r["reviewed_at"] or 0) >= now - REFERENCE_CHECK_DAYS * 86400:
                # Лише описки назв зі словника. Коди доби задано не всі, а позивні й номери поза
                # словником — звичайна справа: про них не нагадуємо.
                issues = [i for i in check_with_db(db, reference, ())["issues"] if i["kind"] == "spelling"]
                if issues:
                    more = f" (+{len(issues) - 1})" if len(issues) > 1 else ""
                    out.append(_suggestion("ref_text", rid, f"{short}: {issues[0]['message']}{more}", record=rid))
    return out


def dictionary_conflicts(db_path):
    """Рядки списків, що суперечать словникам.

    Один номер у кількох позивних — звичайна справа в ефірі, про це не нагадуємо.
    """
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.text_lists import LIST_TYPES, check_against_dictionaries, ensure_tables, load_active
    out = []
    with get_db(db_path) as db:
        ensure_tables(db)
        _, active = load_active(db)
        for list_type in LIST_TYPES:
            errors, _ = check_against_dictionaries(db, list_type, active.get(list_type) or ([] if list_type not in ("aliases", "callsign_aliases") else {}))
            for error in errors:
                out.append(_suggestion("list_conflict", f"{list_type}:{error}", error))
    return out


def collect(db_path, store, *, patterns=None, progress=None, skip=frozenset()):
    """Поточні пропозиції. ``patterns`` — готовий результат глибокої перевірки шаблонів (або None).

    ``skip`` — вимкнені оператором види: їх не обчислюємо (див. ``muted_kinds``)."""
    from core.callsigns import normalize
    from services.alias_candidates import collect as alias_collect
    from services.dictionary_loader import dictionaries_from_lines, read_dictionary_lines
    from services.management.learning import learning_queue
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

    candidates = [] if "alias" in skip else [c for c in alias_collect([{"id": "", "reference": r["reference"], "raw_text": r["raw_text"]} for r in speech],
                                           callsigns, names, active.get("callsign_aliases", {}))
                  # Лише чисті кандидати: без позначок (інший словник, позивний без номера…) і помітно схожі.
                  if c["addable"] and not c["flags"] and c["similarity"] >= MIN_ALIAS_SIMILARITY and c["count"] >= MIN_REPEATS]
    for c, effect in _alias_effects(db_path, store, candidates[:ALIAS_CHECK_MAX], **({"progress": progress} if progress else {})):
        out.append(_suggestion("alias", f"{c['callsign']}:{c['heard']}",
                               f"«{c['heard']}» → {c['callsign']} (×{c['count']}; {effect})", c["count"]))

    noise_keys = {noise_key(v) for v in (*active.get("blacklist", ()), *active.get("short_garbage", ()))}
    for c in ([] if "noise" in skip else noise_collect(records, noise_keys, set(names))):
        if c["addable"] and c["count"] >= MIN_REPEATS and c["reached_text"]:
            out.append(_suggestion("noise", f"{c['list']}:{c['text']}", f"«{c['text']}» (×{c['count']})", c["count"]))

    queue = {"items": []} if {"approval", "conflict"} <= skip else learning_queue(db_path)
    for item in queue["items"]:
        if item["state"] == "needs_approval":
            out.append(_suggestion("approval", f"{item['original']}>{item['corrected']}", f"«{item['original']}» → «{item['corrected']}»"))
        elif item["state"] == "conflict":
            out.append(_suggestion("conflict", item["original"], f"«{item['original']}»"))

    # Номери відомих позивних і нові позивні з еталонів, що повторилися.
    gaps = Counter()
    for r in ([] if "dictionary" in skip else speech):
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
    if any(kind.startswith("ref_") and kind not in skip for kind in KINDS):
        out += reference_checks(db_path, store)
    if "list_conflict" not in skip:
        out += dictionary_conflicts(db_path)
    return [s for s in out if s["kind"] not in skip]


def deep_patterns(db_path, store, *, progress=None):
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
                             after={**current, "patterns": (*current["patterns"], line)}, show=0, progress=progress)
        except Exception:
            continue
        if result["better"] and not result["worse"] and result["after"]["callsigns_false"] <= result["before"]["callsigns_false"]:
            good.append({"pattern": line, "better": result["better"]})
    return good


def refresh(db_path, store, *, deep=False, now=None, progress=None):
    """Оновити стан пропозицій. Повертає (пропозиції, ключі нових)."""
    import json
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        ensure_table(db)
        meta = db.execute("SELECT last_deep, deep_json FROM suggestion_meta WHERE id=1").fetchone()
    last_deep, deep_json = meta if meta else (None, None)
    patterns = json.loads(deep_json) if deep_json else []
    skip = muted_kinds(db_path)
    if "pattern" in skip:
        patterns = []      # найважча перевірка — не запускаємо для вимкненого виду
    elif deep or last_deep is None or now - last_deep >= DEEP_CHECK_HOURS * 3600:
        patterns = deep_patterns(db_path, store, progress=progress)
        last_deep = now
    else:
        # Шаблон, який уже ввімкнули чи видалили, зникає одразу, без глибокої перевірки.
        from services.text_lists import read_lists
        disabled = {e["raw_line"] for e in read_lists(db_path)["lists"]["phrase_patterns"]["entries"] if not e["enabled"]}
        patterns = [p for p in patterns if p["pattern"] in disabled]
    current = collect(db_path, store, patterns=patterns, progress=progress, skip=skip)
    if progress:
        progress(0, 0)
    keys = {s["key"]: s for s in current}
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        known = {row[0] for row in db.execute("SELECT key FROM suggestion_state")}
        # Застосовані чи зниклі пропозиції прибираються; нові — запам’ятовуються з часом появи.
        db.executemany("DELETE FROM suggestion_state WHERE key=?", [(k,) for k in known if k not in keys])
        # Рішення людини стійке: зникнення пропозиції не скасовує «Готово».
        ensure_dismissed(db)
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
        ensure_dismissed(db)
        ensure_muted(db)
        rows = db.execute("""SELECT kind, title, first_seen, record, key FROM suggestion_state
            WHERE key NOT IN (SELECT key FROM suggestion_dismissed)
              AND kind NOT IN (SELECT kind FROM suggestion_muted) ORDER BY first_seen DESC""").fetchall()
        muted = {row[0] for row in db.execute("SELECT kind FROM suggestion_muted")}
        hidden = db.execute("SELECT COUNT(*) FROM suggestion_dismissed").fetchone()[0]
        meta = db.execute("SELECT last_deep FROM suggestion_meta WHERE id=1").fetchone()
    groups = {}
    for kind, title, first_seen, record, key in rows:
        if kind not in KINDS:      # вид, якого вже немає (старий запис стану)
            continue
        name, target, hint = KINDS[kind]
        group = groups.setdefault(kind, {"kind": kind, "title": name, "target": target, "hint": hint,
                                         "count": 0, "new": 0, "examples": [], "records": [], "lines": []})
        # Рядки для чернетки у відповідному редакторі; застосовуються лише після ручного збереження.
        if kind == "alias" and len(group["lines"]) < 50:
            _, callsign, heard = key.split(":", 2)
            group["lines"].append({"list": "callsign_aliases", "callsign": callsign, "line": heard})
        elif kind == "noise" and len(group["lines"]) < 50:
            _, list_type, text = key.split(":", 2)
            group["lines"].append({"list": list_type, "line": text})
        elif kind == "dictionary" and len(group["lines"]) < 50:
            _, callsign, number = key.split(":", 2)
            group["lines"].append({"list": "callsigns", "callsign": callsign, "number": number})
        elif kind == "pattern" and len(group["lines"]) < 50:
            _, pattern = key.split(":", 1)
            group["lines"].append({"list": "phrase_patterns", "line": pattern})
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
    return {"groups": [groups[k] for k in KINDS if k in groups], "checked": meta is not None, "hidden": hidden,
            "kinds": [{"kind": k, "title": KINDS[k][0], "muted": k in muted} for k in KINDS]}


class SuggestionWorker:
    """Фонова перевірка в процесі панелі: раз на CHECK_MINUTES і за кілька секунд після
    правки в панелі (``poke``), без впливу на приймач."""

    def __init__(self, db_path, store, log, *, interval=CHECK_MINUTES * 60, poke_delay=POKE_DELAY_S):
        self.db_path, self.store, self.log, self.interval = db_path, store, log, interval
        self.poke_delay = poke_delay
        self._stop, self._poked = threading.Event(), threading.Event()
        self.busy = False
        self._job_id = None
        self.thread = threading.Thread(target=self._loop, name="suggestions", daemon=True)

    def poke(self):
        """Щось змінилося в панелі — перевірити найближчим часом (без глибокої перевірки шаблонів)."""
        self._poked.set()

    @property
    def pending(self):
        """Перевірка ще йде або запланована: панель покаже актуальне трохи згодом."""
        if self.busy or self._poked.is_set():
            return True
        if self._job_id:
            from services import panel_jobs
            try:
                return panel_jobs.read(self.db_path, self._job_id, include_result=False)["status"] in panel_jobs.ACTIVE
            except (LookupError, OSError, ValueError):
                pass
        return False

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _refresh(self):
        # Усі текстові прогони — в одній збереженій черзі, поза процесом панелі.
        from services import panel_jobs
        self._job_id = panel_jobs.submit(self.db_path, self.store.root, "suggestions")["id"]

    def _loop(self):
        # Невелика затримка: панель і приймач спершу запускаються.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            self._poked.clear()
            self.busy = True
            try:
                self._refresh()
            except (sqlite3.Error, OSError, ValueError) as exc:
                self.log.warning("Пропозиції з розмітки не оновлено: %s", type(exc).__name__)
            except Exception:
                self.log.exception("Помилка перевірки пропозицій з розмітки")
            finally:
                self.busy = False
            deadline = time.monotonic() + self.interval
            while not self._stop.is_set() and not self._poked.is_set() and time.monotonic() < deadline:
                self._poked.wait(min(5, max(0, deadline - time.monotonic())))
            if self._stop.is_set() or (self._poked.is_set() and self._stop.wait(self.poke_delay)):
                return
