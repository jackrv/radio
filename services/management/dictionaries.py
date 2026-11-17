"""Словники позивних, команд і Telegram: читання, перевірка й збереження з версією."""
import re
from callsigns import Callsigns
from database import DICTIONARY_TYPES
from services.sqlite_connection import get_db
from services.user_store import _require_permission
from services.management.common import ManagementConflict
from services.management.common import version


def _dictionary(db, kind):
    rows = db.execute(
        "SELECT raw_line, enabled FROM dictionary_entries "
        "WHERE dictionary_type = ? ORDER BY line_number", (kind,),
    ).fetchall()
    entries = [{"raw_line": line, "enabled": bool(enabled)} for line, enabled in rows]
    return {"kind": kind, "entries": entries, "version": version(entries)}


def read_dictionary(db_path, kind):
    if kind not in DICTIONARY_TYPES:
        raise ValueError("Невідомий словник")
    with get_db(db_path) as db:
        return _dictionary(db, kind)


MAX_DICTIONARY_ENTRIES = 1000


def validate_dictionary(entries, kind):
    if len(entries) > MAX_DICTIONARY_ENTRIES:
        raise ValueError(f"Словник має містити не більше {MAX_DICTIONARY_ENTRIES} записів")
    enabled = []
    expanded_numbers = 0
    for entry in entries:
        line = entry["raw_line"].strip()
        if not line or len(line) > 500 or any(ord(c) < 32 for c in line):
            raise ValueError("Рядок словника має містити від 1 до 500 символів без перенесень")
        content = line.split("#", 1)[0].strip()
        if content:
            # The recognition parser supports one name with optional numeric groups.
            head, colon, tail = content.partition(":")
            if not colon:
                parts = content.split()
                head, tail = parts[0], " ".join(parts[1:])
            if not re.fullmatch(r"[^\W\d_]+(?:[’'ʼ-][^\W\d_]+)*", head.strip()):
                raise ValueError("Назва має бути одним словом; номери вказуються після неї")
            if colon and not tail.strip():
                raise ValueError("Після двокрапки вкажи номери або *")
            if not colon and tail and not re.fullmatch(r"[0-9]{1,9}", tail):
                raise ValueError("Для діапазонів і * використовуй двокрапку після назви")
            for number in re.split(r"[\s,;]+", tail.strip()):
                if not number or number == "*":
                    continue
                if not re.fullmatch(r"[0-9]{1,9}(?:-[0-9]{1,9})?", number):
                    raise ValueError("Допустимі номери, діапазони й *")
                if "-" in number:
                    start, end = map(int, number.split("-"))
                    if not 0 <= end - start <= 10000:
                        raise ValueError("Діапазон має зростати й містити не більше 10001 номера")
                    expanded_numbers += end - start + 1
                else:
                    expanded_numbers += 1
                if expanded_numbers > 50000:
                    raise ValueError("Словник має містити не більше 50000 номерів з урахуванням діапазонів")
            if not colon and len(content.split()) > 2:
                raise ValueError("Для кількох номерів використовуй формат «позивний: 68 69»")
        if entry["enabled"]:
            enabled.append(line)
    if kind == "callsigns" and not Callsigns.from_lines(enabled).names:
        raise ValueError("Не можна залишати приймач без увімкнених позивних")


def write_dictionary(db_path, actor_id, kind, entries, expected_version):
    if kind not in DICTIONARY_TYPES:
        raise ValueError("Невідомий словник")
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "dictionaries.write")
        if _dictionary(db, kind)["version"] != expected_version:
            raise ManagementConflict("Словник змінився. Онови його перед збереженням.")
        validate_dictionary(entries, kind)
        # No other table references these row IDs; the ordered dictionary is one unit.
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type = ?", (kind,))
        db.executemany(
            "INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line, enabled) "
            "VALUES (?, ?, ?, ?)",
            [(kind, i, e["raw_line"].strip(), int(e["enabled"])) for i, e in enumerate(entries, 1)],
        )
        return _dictionary(db, kind)
