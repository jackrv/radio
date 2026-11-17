"""Робочі текстові списки приймача в SQLite, без перезапуску.

- blacklist — цілі фрази-галюцинації Whisper («дякую за перегляд»);
- short_garbage — короткі хибні фрагменти (відкидаються, лише якщо в них
  немає позивних і команд);
- aliases — «правильне: варіант, варіант»: заміни в нормалізованому тексті;
- callsign_aliases — «позивний: почутий варіант»: лише для пошуку позивного;
- whisper_prompt — фрази на початку підказки Whisper, як їх вимовляють
  («дощ 42 42»): допомагають правильно записати важливі команди;
- phrase_templates — обороти м’яких шаблонів («посилку отримав»): схоже на них
  почуте пишеться як в обороті (core/template_text.py);
- phrase_patterns — шаблони цілих фраз («{П}, я {П}, перевірка звʼязку, прийом»):
  фраза, що повністю їм відповідає, пишеться як у шаблоні (core/phrase_patterns.py).

Початкові значення беруться з коду (config.py, core/callsigns.py) один раз — під
час першого створення таблиць. Далі джерело правди — SQLite; редагування лише
в локальній панелі. Приймач перевіряє ревізію не частіше ніж раз на кілька
секунд і застосовує нові списки до наступного сегмента.
"""

import re
import sqlite3
import time

import core.callsigns as callsign_module
from core.callsigns import Callsigns, NUMBER_WORDS, basic_normalize, configure_aliases, normalize
from services.sqlite_connection import get_db

LIST_TYPES = ("blacklist", "short_garbage", "aliases", "callsign_aliases", "whisper_prompt", "phrase_templates",
              "phrase_patterns")
# Списки, що з’явилися пізніше за першу базу: заповнюються початковими значеннями один раз.
LATER_LISTS = ("phrase_templates", "phrase_patterns")
MAX_PATTERN_LINE = 200
MAX_TEMPLATE_WORDS = 6
DISABLED_PREFIX = "#"
MAX_ENTRIES = 500
MAX_LINE = 300
# Підказка Whisper обмежена ~224 токенами; словникова частина вже займає частину.
MAX_PROMPT_PHRASE = 80
MAX_PROMPT_PHRASES_CHARS = 300
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
        "whisper_prompt": [],
        "phrase_templates": list(_default_phrases()),
        # Шаблони фраз додаються вимкненими (#): оператор вмикає їх після перевірки на еталонах.
        "phrase_patterns": [DISABLED_PREFIX + line for line in DEFAULT_PATTERNS],
    }


# Найчастіші схеми передач з еталонів оператора (17.11, 410 передач).
DEFAULT_PATTERNS = (
    "{П}, прийнято, прийом", "{П}, прийом", "{П}, {П}, я {П}, {К} звʼязку, прийом", "{П}, я {П}, {К} звʼязку, прийом",
    "{П}, посилку отримав, прийом", "прийнято, прийом", "{П}, я {П}", "{П}, прийняв, прийом", "{П}, прийнято",
    "{П}, я {П}, роботу закінчив, прийом", "{П}, {П}, я {П}, на звʼязок, прийом", "{П}, я {П}, прийнято, прийом",
    "{П}, {П}, я {П}, прийом", "{П}, посилку отримав", "{П}, я {П}, чую та розбираю вас на пʼятірку, прийнято, прийом",
    "{П}, {П}, я {П}, прийнято, прийом", "{П}, прийняв", "{П}, {П}, я {П}, до роботи приступив, прийом", "{Ч}, прийом",
    "{П}, я {П}, на звʼязку, прийом", "{П}, я {П}, до роботи приступив, прийом", "{П}, чую добре, прийом",
    "{П}, {П}, прийнято, прийом", "{П}, я {П}, чую та розбираю вас на відмінно, прийом",
    "{П}, {П}, я {П}, чую та розбираю вас на відмінно, прийом", "{П}, я {П}, як прийнято, прийом",
)


def _seed_rows(list_type, lines):
    """Рядок із «#» — вимкнений рядок без «#»."""
    return [(list_type, number, line[len(DISABLED_PREFIX):] if line.startswith(DISABLED_PREFIX) else line,
             int(not line.startswith(DISABLED_PREFIX))) for number, line in enumerate(lines, 1)]


def _default_phrases():
    from core.template_text import DEFAULT_PHRASES
    return DEFAULT_PHRASES


def ensure_tables(db):
    """Створює таблиці й один раз заповнює їх початковими значеннями з коду."""
    db.execute(_entries_table_sql("IF NOT EXISTS text_list_entries"))
    try:
        _migrate_list_types(db)
    except sqlite3.OperationalError:
        pass  # знімок лише для читання: старий CHECK не заважає читати рядки
    db.execute("""CREATE TABLE IF NOT EXISTS text_list_state (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS text_list_seeded (list_type TEXT PRIMARY KEY)")
    except sqlite3.OperationalError:
        return  # знімок лише для читання (лабораторія): нічого не дописуємо
    if db.execute("SELECT 1 FROM text_list_state WHERE id = 1").fetchone():
        try:
            _seed_later_lists(db)
        except sqlite3.OperationalError:
            pass
        return
    # Початкове заповнення — лише раз; порожній список потім так і лишається порожнім.
    for list_type, lines in default_lines().items():
        db.executemany("INSERT OR IGNORE INTO text_list_entries (list_type, line_number, raw_line, enabled) VALUES (?, ?, ?, ?)",
                       _seed_rows(list_type, lines))
    db.executemany("INSERT OR IGNORE INTO text_list_seeded (list_type) VALUES (?)", [(t,) for t in LATER_LISTS])
    db.execute("INSERT INTO text_list_state (id, revision) VALUES (1, 1)")


def _seed_later_lists(db):
    """Новий тип списку в наявній базі отримує початкові значення один раз (далі — як збережено)."""
    seeded = {row[0] for row in db.execute("SELECT list_type FROM text_list_seeded")}
    missing = [t for t in LATER_LISTS if t not in seeded]
    if not missing:
        return
    defaults = default_lines()
    for list_type in missing:
        if not db.execute("SELECT 1 FROM text_list_entries WHERE list_type = ?", (list_type,)).fetchone():
            db.executemany("INSERT INTO text_list_entries (list_type, line_number, raw_line, enabled) VALUES (?, ?, ?, ?)",
                           _seed_rows(list_type, defaults[list_type]))
        db.execute("INSERT OR IGNORE INTO text_list_seeded (list_type) VALUES (?)", (list_type,))
    db.execute("UPDATE text_list_state SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE id = 1")


def _entries_table_sql(name):
    return f"""CREATE TABLE {name} (
        id INTEGER PRIMARY KEY,
        list_type TEXT NOT NULL CHECK(list_type IN ({", ".join(repr(t) for t in LIST_TYPES)})),
        line_number INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
        UNIQUE(list_type, line_number)
    )"""


def _migrate_list_types(db):
    """Таблиця зі старим переліком типів у CHECK перебудовується без втрати рядків."""
    def outdated():
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='text_list_entries'").fetchone()
        return row is not None and any(repr(t) not in row[0] for t in LIST_TYPES)

    if not outdated():
        return
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    try:
        if outdated():  # інший процес міг уже перебудувати, поки ми чекали замок
            db.execute("DROP TABLE IF EXISTS text_list_entries_new")
            db.execute(_entries_table_sql("text_list_entries_new"))
            db.execute("INSERT INTO text_list_entries_new (id, list_type, line_number, raw_line, enabled) "
                       "SELECT id, list_type, line_number, raw_line, enabled FROM text_list_entries")
            db.execute("DROP TABLE text_list_entries")
            db.execute("ALTER TABLE text_list_entries_new RENAME TO text_list_entries")
        if own:
            db.commit()
    except BaseException:
        if own:
            db.rollback()
        raise


def compose_prompt(phrases, dictionary_prompt):
    """Фрази зі списку whisper_prompt перед назвами словників: «а 1 1, б 2 2. назви»."""
    phrases = [p.strip().rstrip(".,;") for p in phrases if p.strip().rstrip(".,;")]
    if not phrases:
        return dictionary_prompt
    return f"{', '.join(phrases)}. {dictionary_prompt}".strip()


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

    if list_type == "phrase_patterns":
        from core.phrase_patterns import parse_pattern
        result = []
        for number, line in lines:
            value = " ".join(line.split())
            try:
                if len(value) > MAX_PATTERN_LINE:
                    raise ValueError(f"довше за {MAX_PATTERN_LINE} символів")
                parse_pattern(value)
            except ValueError as exc:
                errors.append(f"Рядок {number}: {exc}")
                continue
            if value not in result:
                result.append(value)
        return result, errors

    if list_type == "phrase_templates":
        result = []
        for number, line in lines:
            value = " ".join(line.lower().replace("'", "ʼ").replace("’", "ʼ").split()).strip(".,;!?")
            words = value.split()
            if not value:
                errors.append(f"Рядок {number}: порожній оборот")
            elif _has_number(value):
                errors.append(f"Рядок {number}: оборот без чисел — числа шаблони не змінюють")
            elif not re.fullmatch(r"[^\W\d_]+(?:[ʼ-][^\W\d_]+)*(?: [^\W\d_]+(?:[ʼ-][^\W\d_]+)*)*", value):
                errors.append(f"Рядок {number}: лише слова, без розділових знаків усередині")
            elif len(words) > MAX_TEMPLATE_WORDS:
                errors.append(f"Рядок {number}: не більше {MAX_TEMPLATE_WORDS} слів")
            elif value not in result:
                result.append(value)
        return result, errors

    if list_type == "whisper_prompt":
        # Регістр і цифри важливі для Whisper: рядок зберігається як є.
        result = []
        for number, line in lines:
            value = " ".join(line.split()).rstrip(".,;")
            if not value:
                errors.append(f"Рядок {number}: порожня фраза")
            elif len(value) > MAX_PROMPT_PHRASE:
                errors.append(f"Рядок {number}: фраза довша за {MAX_PROMPT_PHRASE} символів")
            elif value not in result:
                result.append(value)
        if len(", ".join(result)) > MAX_PROMPT_PHRASES_CHARS:
            errors.append(f"Разом фрази довші за {MAX_PROMPT_PHRASES_CHARS} символів: підказка Whisper обмежена")
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


KIND_NAMES = {"callsigns": "позивних", "commands": "команд", "telegram": "Telegram"}


def _dictionaries(db):
    """Увімкнені словники: {тип: Callsigns}."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dictionary_entries'").fetchone():
        return {}
    groups = {}
    for kind, line in db.execute("SELECT dictionary_type, raw_line FROM dictionary_entries WHERE enabled = 1"):
        groups.setdefault(kind, []).append(line)
    return {kind: Callsigns.from_lines(lines) for kind, lines in groups.items()}


def _dictionary_names(db):
    dictionaries = _dictionaries(db)
    callsigns = set(dictionaries["callsigns"].names) if "callsigns" in dictionaries else set()
    names = set(callsigns)
    for dictionary in dictionaries.values():
        names |= set(dictionary.names)
    return names, callsigns


def _callsign_alias_checks(dictionaries, heard, name):
    """Алиас позивного: (помилки, попередження).

    Позивний переписувати не можна. Слово з іншого словника можна, лише якщо
    алиас веде до позивного з номерами й ці номери не перетинаються: тоді
    «злива 222» лишається командою, а «злива 34» стає позивним.
    """
    errors, warnings = [], []
    callsigns = dictionaries.get("callsigns")
    if callsigns is None or name not in callsigns.names:
        warnings.append(f"«{name}» немає у словнику позивних — алиас не діятиме, доки його не додати")
        return errors, warnings
    if heard in callsigns.names:
        errors.append(f"«{heard}» — правильне словникове ім’я, його не можна переписувати")
        return errors, warnings
    without_number = name in callsigns.standalone or name in callsigns.any_number
    for kind, dictionary in dictionaries.items():
        if kind == "callsigns" or heard not in dictionary.names:
            continue
        where = KIND_NAMES.get(kind, kind)
        if without_number:
            errors.append(f"«{heard}» є у словнику {where}, а «{name}» може бути без номера — "
                          "алиас переписував би це слово скрізь")
            continue
        overlap = sorted(dictionary.numbers.get(heard, set()) & callsigns.numbers[name], key=int)
        if heard in dictionary.any_number:
            overlap = ["будь-який"]
        if overlap:
            errors.append(f"«{heard}» у словнику {where} має ті самі номери, що й «{name}»: {', '.join(overlap[:5])}")
        else:
            warnings.append(f"«{heard}» також є у словнику {where}; алиас діє лише перед номерами «{name}», "
                            "решта значень не зміниться")
    if name in callsigns.standalone and not errors:
        warnings.append(f"«{name}» — позивний без номера: «{heard}» вважатиметься ним у будь-якій фразі")
    return errors, warnings


def check_against_dictionaries(db, list_type, parsed):
    """Захист позивних і команд: помилки (заборонено) і попередження."""
    dictionaries = _dictionaries(db)
    names = set().union(*(set(d.names) for d in dictionaries.values()))
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
    elif list_type == "phrase_patterns":
        from core.phrase_patterns import parse_pattern
        for value in parsed:
            hit = [e[1] for e in parse_pattern(value) if e[0] == "W" and e[1] in names]
            if hit:
                errors.append(f"«{value}»: «{hit[0]}» — назва зі словника; позначте її місцем {{П}} чи {{К}}")
    elif list_type == "phrase_templates":
        callsign_names = set(dictionaries["callsigns"].names) if "callsigns" in dictionaries else set()
        for value in parsed:
            words = value.replace("ʼ", "").split()
            hit = [token for token in words if token in callsign_names]
            if hit:
                errors.append(f"«{value}» містить позивний «{hit[0]}» — шаблон переписував би схожі слова на цей позивний")
                continue
            other = [token for token in words if token in names]
            if other:
                warnings.append(f"«{value}» містить назву зі словника «{other[0]}»: схожі слова писатимуться як ця назва")
        single = [v for v in parsed if " " not in v]
        if single:
            warnings.append("Однослівні звороти (" + ", ".join(f"«{v}»" for v in single[:4]) +
                            ") виправляються лише в кінці фрази або за дуже великої схожості.")
    elif list_type == "whisper_prompt":
        for phrase in parsed:
            words = basic_normalize(phrase).split()
            if words and not any(word in names for word in words):
                warnings.append(f"«{phrase}» не містить жодного слова зі словників")
        if parsed:
            warnings.append("Фрази підказки зрідка потрапляють у розпізнаний текст. "
                            "З’являться хибні спрацьовування — вимкніть рядок через «#».")
    else:
        for heard, name in parsed.items():
            e, w = _callsign_alias_checks(dictionaries, heard, name)
            errors += e
            warnings += w
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


def check_list(db_path, list_type, entries):
    """Ті самі перевірки, що й під час збереження, але без запису: для підказок під час набору."""
    if list_type not in LIST_TYPES:
        raise ValueError("Невідомий список")
    lines = [str(e.get("raw_line", "")).strip() for e in entries if e.get("enabled", True)]
    parsed, errors = parse(list_type, [(n, line) for n, line in enumerate(lines, 1) if line])
    with get_db(db_path) as db:
        ensure_tables(db)
        dictionary_errors, warnings = check_against_dictionaries(db, list_type, parsed)
    return {"errors": errors + dictionary_errors, "warnings": warnings}


def _seeded(db):
    try:
        return {row[0] for row in db.execute("SELECT list_type FROM text_list_seeded")}
    except sqlite3.OperationalError:
        return set()


def load_active(db):
    """Увімкнені значення всіх списків; непридатні рядки пропускаються (fail safe)."""
    ensure_tables(db)
    revision = db.execute("SELECT revision FROM text_list_state WHERE id = 1").fetchone()[0]
    values, seeded = {}, _seeded(db)
    for list_type in LIST_TYPES:
        rows = _lines(db, list_type)
        if list_type in LATER_LISTS and list_type not in seeded and not rows:
            # Старий знімок без цього списку (лабораторія): працюють початкові значення.
            rows = [(raw, on) for _, _, raw, on in _seed_rows(list_type, default_lines()[list_type])]
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
    _configure_templates(values)
    _synced = (path, revision)


def _configure_templates(values):
    from core.phrase_patterns import configure_patterns
    from core.template_text import configure_phrases
    configure_phrases(values.get("phrase_templates", ()))
    configure_patterns(values.get("phrase_patterns", ()))


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
        self.whisper_prompt = ()
        self._listeners = []
        self._checked = None
        self._failed = False

    def on_change(self, callback):
        """Виклик після кожного застосування нової ревізії (у потоці розпізнавання)."""
        self._listeners.append(callback)

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
        self.whisper_prompt = tuple(values.get("whisper_prompt", ()))
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
        for callback in self._listeners:
            try:
                callback()
            except Exception:
                if self.log is not None:
                    self.log.exception("Не вдалося застосувати оновлені текстові списки")
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
