"""Transactional management operations for the authenticated local Mini App."""

import hashlib
import json
import re
import time

import config
from callsigns import Callsigns
from database import DICTIONARY_TYPES
from services.sqlite_connection import get_db
from services.user_store import _require_permission, _valid_id
from services.word_store import refresh_word_rule_statuses
from services.telegram_phrase_store import ensure_table, RETENTION_SECONDS
from services.telegram_media_store import ensure_table as ensure_media_table
from services.user_profiles import ensure_table as ensure_profiles, clean_name


class ManagementConflict(Exception):
    pass


def version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


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


def list_phrases(db_path, query="", offset=0, limit=20):
    # Коротка окрема транзакція очищення: пошук нижче не тримає блокування запису,
    # тож перегляд журналу не затримує збереження нових подій приймачем.
    with get_db(db_path) as db:
        ensure_table(db)
        ensure_media_table(db)
        db.execute("DELETE FROM telegram_recent_phrases WHERE created_at <= ?",
                   (int(time.time()) - RETENTION_SECONDS,))
        db.execute("""DELETE FROM telegram_phrase_messages WHERE fix_id LIKE 'mem:%'
            AND NOT EXISTS (SELECT 1 FROM telegram_recent_phrases p WHERE 'mem:' || p.token=fix_id)""")
    with get_db(db_path) as db:
        db.create_function("casefold", 1, lambda text: (text or "").casefold())
        rows = db.execute("""
            SELECT * FROM (
                SELECT 'db:' || id AS fix_id, COALESCE(raw_text, text) AS original_text,
                       text AS current_text, ts AS created_at
                FROM radio_events
                UNION ALL
                SELECT 'mem:' || token, original_text, current_text,
                       datetime(created_at, 'unixepoch') FROM telegram_recent_phrases
            ) AS phrases WHERE (instr(casefold(original_text), ?) > 0 OR instr(casefold(current_text), ?) > 0)
              AND NOT EXISTS (SELECT 1 FROM telegram_phrase_messages m
                  WHERE m.fix_id=phrases.fix_id AND m.state<>'visible')
            ORDER BY created_at DESC, fix_id DESC LIMIT ? OFFSET ?
        """, (query.casefold(), query.casefold(), limit + 1, offset)).fetchall()
        media = {row[0]: bool(db.execute(
            "SELECT 1 FROM telegram_phrase_messages WHERE fix_id=? AND state='visible' AND voice_file_id IS NOT NULL",
            (row[0],)).fetchone()) for row in rows[:limit]}
    return {"items": [{**dict(zip(("fix_id", "original_text", "current_text", "created_at"), r)),
                       "has_audio": bool(media.get(r[0]))} for r in rows[:limit]], "has_more": len(rows) > limit}


def list_rules(db_path, query="", offset=0, limit=20):
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT id, original, corrected, confirmations, status FROM word_corrections "
            "WHERE instr(original, ?) > 0 OR instr(corrected, ?) > 0 "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (query.lower(), query.lower(), limit + 1, offset),
        ).fetchall()
    keys = ("id", "original", "corrected", "confirmations", "status")
    return {"items": [{**dict(zip(keys, row)), "version": version(row)} for row in rows[:limit]],
            "has_more": len(rows) > limit}


def toggle_rule(db_path, actor_id, rule_id, disabled, expected_version):
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "dictionaries.write")
        row = db.execute(
            "SELECT id, original, corrected, confirmations, status FROM word_corrections WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Правило не знайдено")
        if version(row) != expected_version:
            raise ManagementConflict("Правило змінилося. Онови список.")
        db.execute("UPDATE word_corrections SET status = ? WHERE id = ?",
                   ("disabled" if disabled else "pending", rule_id))
        # Re-enabling never bypasses confirmation or conflict checks.
        refresh_word_rule_statuses(db)
    return {"saved": True}


def list_users(db_path, actor_id):
    with get_db(db_path) as db:
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        rows = db.execute("""SELECT u.user_id, u.role, u.enabled, COALESCE(p.display_name,''),
            COALESCE(p.telegram_name,''), COALESCE(p.username,'') FROM telegram_users u
            LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id ORDER BY u.user_id""").fetchall()
    return {"items": [{"user_id": str(uid), "role": role, "enabled": bool(enabled),
                       "display_name": custom, "telegram_name": name, "username": username,
                       "name": custom or name or ("@" + username if username else f"Telegram {uid}"),
                       "version": version((uid, role, enabled, custom))}
                      for uid, role, enabled, custom, name, username in rows]}


def write_user(db_path, actor_id, user_id, role, enabled, expected_version, display_name=None):
    _valid_id(user_id)
    if role not in ("admin", "editor", "viewer"):
        raise ValueError("Невідома роль")
    if display_name is not None:
        display_name = clean_name(display_name)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        row = db.execute("SELECT user_id, role, enabled FROM telegram_users WHERE user_id = ?",
                         (user_id,)).fetchone()
        profile = db.execute("SELECT display_name FROM telegram_user_profiles WHERE user_id=?", (user_id,)).fetchone()
        if (version((*row, profile[0] if profile else "")) if row else None) != expected_version:
            raise ManagementConflict("Користувач змінився. Онови список.")
        if row and row[1:] == ("admin", 1) and (role != "admin" or not enabled):
            admins = db.execute("SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError("Не можна вимкнути або понизити останнього адміністратора")
        db.execute("""
            INSERT INTO telegram_users (user_id, role, enabled) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, enabled=excluded.enabled
        """, (user_id, role, int(enabled)))
        if display_name is not None:
            db.execute("""INSERT INTO telegram_user_profiles (user_id,display_name) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name""", (user_id, display_name))
    return {"saved": True}


SETTING_KEYS = ("USE_LLM", "VAD_LEVEL", "SILENCE_END_MS", "MIN_SPEECH_MS", "MAX_SEGMENT_S")


def _settings(db):
    db.execute("""CREATE TABLE IF NOT EXISTS recognition_settings (
        id INTEGER PRIMARY KEY CHECK (id=1), values_json TEXT NOT NULL, revision INTEGER NOT NULL
    )""")
    row = db.execute("SELECT values_json, revision FROM recognition_settings WHERE id=1").fetchone()
    values = {key: getattr(config, key) for key in SETTING_KEYS}
    if row:
        values.update(json.loads(row[0]))
    return {"values": values, "revision": row[1] if row else 0,
            "requires_restart": True, "whisper_model": config.MODEL_REPO,
            "correction_model": config.LLM_REPO, "language": config.LANGUAGE}


def read_settings(db_path):
    with get_db(db_path) as db:
        return _settings(db)


def write_settings(db_path, actor_id, values, revision):
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "settings.write")
        if _settings(db)["revision"] != revision:
            raise ManagementConflict("Налаштування змінилися. Онови сторінку.")
        db.execute("""INSERT INTO recognition_settings (id, values_json, revision) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json, revision=excluded.revision
        """, (json.dumps(values), revision + 1))
        return _settings(db)


def apply_startup_settings(db_path, target):
    settings = read_settings(db_path)["values"]
    for key in SETTING_KEYS:
        setattr(target, key, settings[key])


def overview(db_path):
    with get_db(db_path) as db:
        events = db.execute("SELECT COUNT(*), MAX(ts) FROM radio_events").fetchone()
        rules = db.execute("SELECT COUNT(*) FROM word_corrections WHERE status='active'").fetchone()[0]
        dictionaries = dict(db.execute("SELECT dictionary_type, COUNT(*) FROM dictionary_entries WHERE enabled=1 GROUP BY dictionary_type"))
    return {"events": events[0], "last_event_at": events[1], "active_rules": rules,
            "dictionary_entries": sum(dictionaries.values()), "dictionaries": dictionaries}
