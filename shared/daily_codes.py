"""Цифрові коди доби, які оператор вносить вручну («48 62, 31 05»), і постійні коди.

У межах доби група позивних повторює ті самі цифрові комбінації. Коди не
вгадуються з розпізнаного тексту: там трапляються й помилки Whisper, і номери
позивних. Тут вони лише зберігаються за датою (місцевий день) і віддаються
перевірці тексту, яка підказує оператору «Можливо, 48 62?».
"""

import datetime
import json
import re
import time

from shared.sqlite_connection import get_db

MAX_CODES = 20
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_CODE_COMMA = re.compile(r"(?<!\d)(?P<first>\d{2})\s*,\s*(?P<second>\d{2})(?!\d)")


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS daily_codes (
        day TEXT PRIMARY KEY,
        codes TEXT NOT NULL DEFAULT '[]',
        updated_at REAL NOT NULL)""")


def parse_codes(text):
    """«48 62, 3105; 72-59; 48,62» -> ["48 62", "31 05", "72 59"]. ValueError з поясненням.

    Групи цифр беруться по черзі: чотири цифри — код; дві пари поспіль — теж код.
    """
    if re.search(r"[^\d\s,;.\-–]", text or ""):
        raise ValueError("Лише цифри: коди через кому, як «48 62, 31 05»")
    groups = re.findall(r"\d+", text or "")
    codes, i = [], 0
    while i < len(groups):
        if len(groups[i]) == 4:
            digits, i = groups[i], i + 1
        elif len(groups[i]) == 2 and i + 1 < len(groups) and len(groups[i + 1]) == 2:
            digits, i = groups[i] + groups[i + 1], i + 2
        else:
            raise ValueError(f"«{groups[i]}» — код має бути з чотирьох цифр, як «48 62»")
        code = f"{digits[:2]} {digits[2:]}"
        if code not in codes:
            codes.append(code)
    if len(codes) > MAX_CODES:
        raise ValueError(f"Не більше {MAX_CODES} кодів на добу")
    return codes


def remove_confirmed_code_commas(text, codes, dictionaries=()):
    """Замінює кому між парами цифр лише для точного коду з переліку доби."""
    valid = set()
    for code in codes or ():
        digits = "".join(re.findall(r"\d", str(code)))
        if digits.isdigit() and len(digits) == 4:
            valid.add(digits)
    if not valid or not text:
        return text

    from shared.callsigns import CALLSIGN_ALIASES

    def is_dictionary_number_before(start, number):
        prefix = text[:start]
        for dictionary in dictionaries or ():
            names = getattr(dictionary, "names", ())
            numbers = getattr(dictionary, "numbers", {})
            any_number = getattr(dictionary, "any_number", ())
            for name in names:
                if number not in numbers.get(name, ()) and name not in any_number:
                    continue
                variants = (name, *(heard for heard, canonical in CALLSIGN_ALIASES.items()
                                    if canonical == name))
                for variant in variants:
                    name_pattern = r"\s+".join(re.escape(part) for part in variant.split())
                    pattern = rf"(?<![\w]){name_pattern}\s*[,;:]?\s*$"
                    if re.search(pattern, prefix, re.IGNORECASE):
                        return True
        return False

    replacements = []
    position = 0
    while match := _CODE_COMMA.search(text, position):
        first, second = match.group("first"), match.group("second")
        if first + second in valid and not is_dictionary_number_before(match.start(), first):
            replacements.append((match.start(), match.end(), f"{first} {second}"))
            position = match.end()
        else:
            position = match.start("first") + len(first)
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def change_hour():
    """Година зміни кодів (місцевий час): коди «доби Д» діють з Д−1 о цій годині до Д."""
    import config
    return int(config.DAILY_CODES_CHANGE_HOUR)


def local_day(moment=None):
    """Доба кодів «РРРР-ММ-ДД» для моменту (unix-час, datetime або None — зараз).

    Коди змінюються не опівночі, а о change_hour(): запис 16.11 о 23:05 належить
    добі кодів 17.11 (з 16.11 20:00 до 17.11 20:00).
    """
    if isinstance(moment, (int, float)):
        moment = datetime.datetime.fromtimestamp(moment)
    moment = moment or datetime.datetime.now()
    if moment.hour >= change_hour():
        moment += datetime.timedelta(days=1)
    return moment.strftime("%Y-%m-%d")


def period(day):
    """(початок, кінець) доби кодів як рядки «16.11 20:00» для показу."""
    end = datetime.datetime.combine(datetime.date.fromisoformat(check_day(day)), datetime.time(change_hour()))
    start = end - datetime.timedelta(days=1)
    return start.strftime("%d.%m %H:%M"), end.strftime("%d.%m %H:%M")


def day_of_event_ts(value):
    """radio_events.ts — UTC «РРРР-ММ-ДД ГГ:ХХ:СС» -> місцевий день."""
    moment = datetime.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    return local_day(moment.timestamp())


def check_day(day):
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError("Дата має бути у форматі РРРР-ММ-ДД")
    datetime.date.fromisoformat(day)
    return day


PERMANENT = "permanent"   # рядок таблиці з постійними кодами, що діють завжди


def read_permanent(db):
    ensure_table(db)
    row = db.execute("SELECT codes FROM daily_codes WHERE day=?", (PERMANENT,)).fetchone()
    return json.loads(row[0]) if row else []


def read_day(db, day):
    """Лише коди цієї доби, без постійних."""
    ensure_table(db)
    row = db.execute("SELECT codes FROM daily_codes WHERE day=?", (check_day(day),)).fetchone()
    return json.loads(row[0]) if row else []


def read(db, day):
    """Коди, що діють у цю добу: постійні й коди доби."""
    codes = read_permanent(db)
    return codes + [c for c in read_day(db, day) if c not in codes]


def write_permanent(db_path, text):
    codes = parse_codes(text)
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute("INSERT INTO daily_codes (day, codes, updated_at) VALUES (?,?,?) "
                   "ON CONFLICT(day) DO UPDATE SET codes=excluded.codes, updated_at=excluded.updated_at",
                   (PERMANENT, json.dumps(codes, ensure_ascii=False), time.time()))
    return codes


def write(db_path, day, text):
    codes = parse_codes(text)
    with get_db(db_path) as db:
        ensure_table(db)
        if codes:
            db.execute("INSERT INTO daily_codes (day, codes, updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(day) DO UPDATE SET codes=excluded.codes, updated_at=excluded.updated_at",
                       (check_day(day), json.dumps(codes, ensure_ascii=False), time.time()))
        else:
            db.execute("DELETE FROM daily_codes WHERE day=?", (check_day(day),))
    return codes


def recent(db_path, days=14):
    with get_db(db_path) as db:
        ensure_table(db)
        rows = db.execute("SELECT day, codes FROM daily_codes WHERE day<>? ORDER BY day DESC LIMIT ?",
                          (PERMANENT, days)).fetchall()
    return [{"day": day, "codes": json.loads(codes), "period": period(day)} for day, codes in rows]


def for_fix_id(db, fix_id):
    """Коди доби події чи тимчасової фрази Telegram; без дати — коди сьогодні."""
    day = local_day()
    try:
        if fix_id and fix_id.startswith("db:"):
            row = db.execute("SELECT ts FROM radio_events WHERE id=?", (int(fix_id[3:]),)).fetchone()
            if row and row[0]:
                day = day_of_event_ts(row[0])
        elif fix_id and fix_id.startswith("mem:"):
            row = db.execute("SELECT created_at FROM telegram_recent_phrases WHERE token=?", (fix_id[4:],)).fetchone()
            if row and row[0]:
                day = local_day(float(row[0]))
    except Exception:
        pass
    return read(db, day)
