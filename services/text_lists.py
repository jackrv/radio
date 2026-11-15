"""Робочі текстові списки приймача в SQLite, без перезапуску.

- blacklist — цілі фрази-галюцинації Whisper («дякую за перегляд»);
- short_garbage — короткі хибні фрагменти (відкидаються, лише якщо в них
  немає позивних і команд);
- aliases — «правильне: варіант, варіант»: заміни в нормалізованому тексті;
- callsign_aliases — «позивний: почутий варіант»: лише для пошуку позивного.

Початкові значення беруться з коду (config.py, callsigns.py) один раз — під
час першого створення таблиць. Далі джерело правди — SQLite; редагування лише
в локальній панелі. Приймач перевіряє ревізію не частіше ніж раз на кілька
секунд і застосовує нові списки до наступного сегмента.
"""

import re
import sqlite3
import time

import callsigns as callsign_module
from callsigns import Callsigns, NUMBER_WORDS, basic_normalize, configure_aliases, normalize
from services.sqlite_connection import get_db

LIST_TYPES = ("blacklist", "short_garbage", "aliases", "callsign_aliases")
DISABLED_PREFIX = "#"
MAX_ENTRIES = 500
MAX_LINE = 300
REFRESH_INTERVAL_S = 2.0


class TextListConflict(Exception):
    pass


def default_lines():
    import config
    callsign_groups = {}
    for heard, name in callsign_module.DEFAULT_CALLSIGN_ALIASES.items():
        callsign_groups.setdefault(name, []).append(heard)
    return {
        "blacklist": list(getattr(config, "BLACKLIST", ())),
        "short_garbage": sorted(getattr(config, "SHORT_GARBAGE", ())),
        "aliases": [f"{canon}: {', '.join(variants)}" for canon, variants in callsign_module.DEFAULT_ALIASES.items()],
        "callsign_aliases": [f"{name}: {', '.join(heard)}" for name, heard in callsign_groups.items()],
    }


def ensure_tables(db):
    """Створює таблиці й один раз заповнює їх початковими значеннями з коду."""
    db.execute(f"""CREATE TABLE IF NOT EXISTS text_list_entries (
        id INTEGER PRIMARY KEY,
        list_type TEXT NOT NULL CHECK(list_type IN ({", ".join(repr(t) for t in LIST_TYPES)})),
        line_number INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
        UNIQUE(list_type, line_number)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS text_list_state (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    if db.execute("SELECT 1 FROM text_list_state WHERE id = 1").fetchone():
        return
    # Початкове заповнення — лише раз; порожній список потім так і лишається порожнім.
    for list_type, lines in default_lines().items():
        db.executemany("INSERT OR IGNORE INTO text_list_entries (list_type, line_number, raw_line) VALUES (?, ?, ?)",
                       [(list_type, number, line) for number, line in enumerate(lines, 1)])
    db.execute("INSERT INTO text_list_state (id, revision) VALUES (1, 1)")


def _has_number(value):
    return any(ch.isdigit() for ch in value) or any(token in NUMBER_WORDS for token in value.split())


def parse(list_type, lines):
    """Розбирає увімкнені рядки. Повертає (значення, помилки з номерами рядків)."""
    errors = []
    if list_type in ("blacklist", "short_garbage"):
        result = []
        for number, line in lines:
            value = basic_normalize(line)
            if not value:
                errors.append(f"Рядок {number}: порожня фраза")
            elif _has_number(value):
                errors.append(f"Рядок {number}: фраза з числом може бути справжнім повідомленням")
            elif value not in result:
                result.append(value)
        return result, errors

    if list_type not in ("aliases", "callsign_aliases"):
        raise ValueError("Невідомий список")
    result, owner = {}, {}
    for number, line in lines:
        if ":" not in line:
            errors.append(f"Рядок {number}: потрібен формат «правильне: варіант, варіант»")
            continue
        head, tail = line.split(":", 1)
        canon = basic_normalize(head)
        variants = [basic_normalize(part) for part in tail.split(",")]
        variants = [v for v in variants if v]
        if not canon or not variants:
            errors.append(f"Рядок {number}: потрібні і правильне написання, і хоча б один варіант")
            continue
        if _has_number(canon) or any(_has_number(v) for v in variants):
            errors.append(f"Рядок {number}: числа не можна задавати алиасами")
            continue
        if list_type == "callsign_aliases" and (" " in canon or any(" " in v for v in variants)):
            errors.append(f"Рядок {number}: позивний і його варіанти — по одному слову")
            continue
        for variant in variants:
            if variant == canon:
                errors.append(f"Рядок {number}: варіант «{variant}» збігається з правильним написанням")
            elif owner.get(variant, canon) != canon:
                errors.append(f"Рядок {number}: варіант «{variant}» уже веде до «{owner[variant]}»")
            else:
                owner[variant] = canon
                if list_type == "aliases":
                    result.setdefault(canon, [])
                    if variant not in result[canon]:
                        result[canon].append(variant)
                else:
                    result[variant] = canon
    return result, errors


def _dictionary_names(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dictionary_entries'").fetchone():
        return set(), set()
    groups = {}
    for kind, line in db.execute("SELECT dictionary_type, raw_line FROM dictionary_entries WHERE enabled = 1"):
        groups.setdefault(kind, []).append(line)
    callsigns = set(Callsigns.from_lines(groups.get("callsigns", [])).names)
    names = set(callsigns)
    for kind, lines in groups.items():
        names |= set(Callsigns.from_lines(lines).names)
    return names, callsigns


def check_against_dictionaries(db, list_type, parsed):
    """Захист позивних і команд: помилки (заборонено) і попередження."""
    names, callsigns = _dictionary_names(db)
    errors, warnings = [], []
    if list_type in ("blacklist", "short_garbage"):
        for value in parsed:
            hit = [token for token in value.split() if token in names]
            if hit:
                errors.append(f"«{value}» містить словникове ім’я «{hit[0]}» — справжні повідомлення відкидалися б")
    elif list_type == "aliases":
        for canon, variants in parsed.items():
            for variant in variants:
                if variant in names:
                    errors.append(f"«{variant}» — правильне словникове ім’я, його не можна переписувати")
    else:
        for heard, name in parsed.items():
            if heard in names:
                errors.append(f"«{heard}» — правильне словникове ім’я, його не можна переписувати")
            if name not in callsigns:
                warnings.append(f"«{name}» немає у словнику позивних — алиас не діятиме, доки його не додати")
    return errors, warnings


def _lines(db, list_type):
    return db.execute("SELECT raw_line, enabled FROM text_list_entries WHERE list_type = ? ORDER BY line_number",
                      (list_type,)).fetchall()


def read_lists(db_path):
    with get_db(db_path) as db:
        ensure_tables(db)
        revision = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone()[0]
        lists = {}
        for list_type in LIST_TYPES:
            rows = _lines(db, list_type)
            parsed, _ = parse(list_type, [(n, line) for n, (line, on) in enumerate(rows, 1) if on])
            lists[list_type] = {
                "entries": [{"raw_line": line, "enabled": bool(on)} for line, on in rows],
                "warnings": check_against_dictionaries(db, list_type, parsed)[1],
            }
    return {"revision": revision, "lists": lists}


def write_list(db_path, list_type, entries, expected_revision):
    """Атомарно замінює один список. Помилки валідації — ValueError з усіма рядками."""
    if list_type not in LIST_TYPES:
        raise ValueError("Невідомий список")
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"Не більше {MAX_ENTRIES} рядків")
    clean = []
    for entry in entries:
        line = str(entry.get("raw_line", "")).strip()
        if len(line) > MAX_LINE:
            raise ValueError(f"Рядок довший за {MAX_LINE} символів")
        if line:
            clean.append((line, bool(entry.get("enabled", True))))
    parsed, errors = parse(list_type, [(n, line) for n, (line, on) in enumerate(clean, 1) if on])
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_tables(db)
        revision = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone()[0]
        if revision != expected_revision:
            raise TextListConflict("Списки змінено в іншому вікні. Онови сторінку.")
        dictionary_errors, warnings = check_against_dictionaries(db, list_type, parsed)
        errors += dictionary_errors
        if errors:
            raise ValueError("; ".join(errors[:10]))
        db.execute("DELETE FROM text_list_entries WHERE list_type = ?", (list_type,))
        db.executemany("INSERT INTO text_list_entries (list_type, line_number, raw_line, enabled) VALUES (?, ?, ?, ?)",
                       [(list_type, n, line, int(on)) for n, (line, on) in enumerate(clean, 1)])
        db.execute("UPDATE text_list_state SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE id = 1")
        revision += 1
    return {"saved": True, "revision": revision, "warnings": warnings}


def load_active(db):
    """Увімкнені значення всіх списків; непридатні рядки пропускаються (fail safe)."""
    ensure_tables(db)
    revision = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone()[0]
    values = {}
    for list_type in LIST_TYPES:
        rows = _lines(db, list_type)
        values[list_type], _ = parse(list_type, [(n, line) for n, (line, on) in enumerate(rows, 1) if on])
    return revision, values


def blacklist_key(value):
    return " ".join(re.findall(r"[^\W_]+", normalize(value), flags=re.UNICODE))


_synced = None


def sync_aliases(db):
    """Для API-процесів: застосувати актуальні алиаси, якщо ревізія змінилася."""
    global _synced
    ensure_tables(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    revision = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone()[0]
    if _synced == (path, revision):
        return
    revision, values = load_active(db)
    configure_aliases(values["aliases"], values["callsign_aliases"])
    _synced = (path, revision)


class LiveTextLists:
    """Знімок списків для потоку розпізнавання. Помилка бази — лишаємо попередні списки."""

    def __init__(self, db_path, log=None, *, interval=REFRESH_INTERVAL_S, clock=time.monotonic):
        self.db_path = db_path
        self.log = log
        self.interval = interval
        self.clock = clock
        self.revision = None
        self.blacklist = frozenset()
        self.short_garbage = frozenset()
        self._checked = None
        self._failed = False

    def refresh(self, force=False):
        now = self.clock()
        if not force and self._checked is not None and now - self._checked < self.interval:
            return False
        self._checked = now
        try:
            with get_db(self.db_path) as db:
                row = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone() \
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='text_list_state'").fetchone() else None
                if row is not None and row[0] == self.revision:
                    return False
                revision, values = load_active(db)
        except sqlite3.Error:
            if self.log is not None and not self._failed:
                self.log.warning("Текстові списки недоступні; використовую попередні")
            self._failed = True
            return False
        self._failed = False
        configure_aliases(values["aliases"], values["callsign_aliases"])
        # Ключі рахуються вже з новими алиасами — так само, як для розпізнаного тексту.
        self.blacklist = frozenset(k for k in (blacklist_key(v) for v in values["blacklist"]) if k)
        self.short_garbage = frozenset(normalize(v) for v in values["short_garbage"])
        changed = self.revision is not None
        self.revision = revision
        try:
            from core.diagnostics import runtime
            runtime.gauge("text_lists.revision", revision)
        except Exception:
            pass
        if self.log is not None:
            self.log.info("Текстові списки %s (ревізія %d): фраз %d, шуму %d, алиасів %d, алиасів позивних %d",
                          "оновлено" if changed else "завантажено", revision, len(self.blacklist),
                          len(self.short_garbage), len(values["aliases"]), len(values["callsign_aliases"]))
        return True

    def short_garbage_view(self):
        return _LiveSet(self, "short_garbage")


class _LiveSet:
    """Множина, що завжди дивиться на поточний знімок (для EventProcessor)."""

    def __init__(self, owner, attribute):
        self._owner, self._attribute = owner, attribute

    def __contains__(self, value):
        return value in getattr(self._owner, self._attribute)

    def __iter__(self):
        return iter(getattr(self._owner, self._attribute))

    def __len__(self):
        return len(getattr(self._owner, self._attribute))
