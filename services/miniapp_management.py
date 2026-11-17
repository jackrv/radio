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
from services.user_notify_store import ensure_table as ensure_notify, effective_mode, allowed_modes


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


_RULE_COLUMNS = "id, original, corrected, confirmations, status, approved_at"


def _rule_item(row, names):
    from core.word_rule_validation import classify_pair
    item = dict(zip(("id", "original", "corrected", "confirmations", "status", "approved_at"), row))
    kind, reason = classify_pair(item["original"], item["corrected"], names)
    # callsign — лише перед дозволеним номером; word — після дозволу оператора; blocked — ніколи.
    item["kind"] = kind if kind == "callsign" else "word" if kind in ("word", "review") else "blocked"
    item["blocked_reason"] = reason if item["kind"] == "blocked" else None
    item["approved"] = item.pop("approved_at") is not None
    item["applies"] = (item["status"] == "active" and item["kind"] != "blocked"
                       and (item["kind"] == "callsign" or item["approved"]))
    item["needs_approval"] = item["kind"] == "word" and not item["approved"]
    item["version"] = version(row)
    return item


def list_rules(db_path, query="", offset=0, limit=20):
    from services.word_store import dictionary_names
    with get_db(db_path) as db:
        names = dictionary_names(db)
        rows = db.execute(
            f"SELECT {_RULE_COLUMNS} FROM word_corrections "
            "WHERE instr(original, ?) > 0 OR instr(corrected, ?) > 0 "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (query.lower(), query.lower(), limit + 1, offset),
        ).fetchall()
    return {"items": [_rule_item(row, names) for row in rows[:limit]],
            "has_more": len(rows) > limit}


def _rule_state(item, min_confirmations):
    """Чому правило діє або не діє — одним словом для панелі."""
    if item["kind"] == "blocked":
        return "blocked"
    if item["status"] == "disabled":
        return "disabled"
    if item["applies"]:
        return "applies"
    if item["confirmations"] < min_confirmations:
        return "needs_confirmation"
    if item["status"] != "active":
        return "conflict"           # підтверджень досить, але є інший варіант того самого слова
    return "needs_approval"


def learning_queue(db_path):
    """Усі навчені заміни з причиною, чому кожна діє чи ні, і правками-джерелами (лише читання)."""
    from services.word_store import MIN_CONFIRMATIONS, dictionary_names, init_word_db
    init_word_db(db_path)
    with get_db(db_path) as db:
        names = dictionary_names(db)
        rows = db.execute(f"SELECT {_RULE_COLUMNS}, updated_at FROM word_corrections ORDER BY id DESC").fetchall()
        sources = {}
        for source_id, original, corrected in db.execute(
                "SELECT source_id, original, corrected FROM word_correction_votes ORDER BY rowid"):
            sources.setdefault((original, corrected), []).append(source_id)
    items, counts = [], {}
    for row in rows:
        item = _rule_item(row[:6], names)
        item["updated_at"] = row[6]
        item["state"] = _rule_state(item, MIN_CONFIRMATIONS)
        item["sources"] = sources.get((item["original"], item["corrected"]), [])
        counts[item["state"]] = counts.get(item["state"], 0) + 1
        items.append(item)
    return {"items": items, "counts": counts, "total": len(items), "min_confirmations": MIN_CONFIRMATIONS}


def _locked_rule(db, actor_id, rule_id, expected_version):
    _require_permission(db, actor_id, "dictionaries.write")
    row = db.execute(f"SELECT {_RULE_COLUMNS} FROM word_corrections WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise LookupError("Правило не знайдено")
    if version(row) != expected_version:
        raise ManagementConflict("Правило змінилося. Онови список.")
    return row


def toggle_rule(db_path, actor_id, rule_id, disabled, expected_version):
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _locked_rule(db, actor_id, rule_id, expected_version)
        db.execute("UPDATE word_corrections SET status = ? WHERE id = ?",
                   ("disabled" if disabled else "pending", rule_id))
        # Re-enabling never bypasses confirmation or conflict checks.
        refresh_word_rule_statuses(db)
    return {"saved": True}


def approve_rule(db_path, actor_id, rule_id, approved, expected_version):
    """Дозвіл застосовувати правило звичайного слова. Підтвердження й конфлікти діють як раніше."""
    from services.word_store import dictionary_names
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = _locked_rule(db, actor_id, rule_id, expected_version)
        if approved and _rule_item(row, dictionary_names(db))["kind"] != "word":
            raise ValueError("Дозвіл потрібен лише для правил звичайних слів")
        db.execute("UPDATE word_corrections SET approved_at = ?, approved_by = ? WHERE id = ?",
                   (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) if approved else None,
                    actor_id if approved else None, rule_id))
    return {"saved": True}


def list_users(db_path, actor_id):
    with get_db(db_path) as db:
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        ensure_notify(db)
        rows = db.execute("""SELECT u.user_id, u.role, u.enabled, COALESCE(p.display_name,''),
            COALESCE(p.telegram_name,''), COALESCE(p.username,''), s.notify_mode FROM telegram_users u
            LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id
            LEFT JOIN telegram_user_settings s ON s.user_id=u.user_id ORDER BY u.user_id""").fetchall()
    return {"items": [{"user_id": str(uid), "role": role, "enabled": bool(enabled),
                       "display_name": custom, "telegram_name": name, "username": username,
                       "name": custom or name or ("@" + username if username else f"Telegram {uid}"),
                       "notify_mode": effective_mode(role, mode),
                       "version": version((uid, role, enabled, custom, mode))}
                      for uid, role, enabled, custom, name, username, mode in rows]}


def write_user(db_path, actor_id, user_id, role, enabled, expected_version, display_name=None, notify_mode=None):
    _valid_id(user_id)
    if role not in ("admin", "editor", "viewer"):
        raise ValueError("Невідома роль")
    if display_name is not None:
        display_name = clean_name(display_name)
    if notify_mode is not None and notify_mode not in allowed_modes(role):
        raise ValueError("Цей режим сповіщень недоступний для вибраної ролі")
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        ensure_notify(db)
        row = db.execute("SELECT user_id, role, enabled FROM telegram_users WHERE user_id = ?",
                         (user_id,)).fetchone()
        profile = db.execute("SELECT display_name FROM telegram_user_profiles WHERE user_id=?", (user_id,)).fetchone()
        mode_row = db.execute("SELECT notify_mode FROM telegram_user_settings WHERE user_id=?", (user_id,)).fetchone()
        stored_mode = mode_row[0] if mode_row else None
        if (version((*row, profile[0] if profile else "", stored_mode)) if row else None) != expected_version:
            raise ManagementConflict("Користувач змінився. Онови список.")
        if row and row[1:] == ("admin", 1) and (role != "admin" or not enabled):
            admins = db.execute("SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError("Не можна вимкнути або понизити останнього адміністратора")
        db.execute("""
            INSERT INTO telegram_users (user_id, role, enabled) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, enabled=excluded.enabled
        """, (user_id, role, int(enabled)))
        if notify_mode is not None:
            db.execute("""INSERT INTO telegram_user_settings VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET notify_mode=excluded.notify_mode""", (user_id, notify_mode))
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


def learning_overview(db):
    """Окремі лічильники навчання — не змішуються між собою."""
    from services.word_store import dictionary_names
    names = dictionary_names(db)
    rows = db.execute(f"SELECT {_RULE_COLUMNS} FROM word_corrections").fetchall()
    items = [_rule_item(row, names) for row in rows]
    candidates = [i for i in items if i["confirmations"] > 0 and i["status"] != "disabled"]
    return {
        "candidates": len(candidates),
        "confirmations": sum(i["confirmations"] for i in candidates),
        "active_rules": sum(i["applies"] for i in items),
        "awaiting_approval": sum(i["status"] == "active" and i["needs_approval"] for i in items),
        "waiting_confirmations": sum(i["status"] == "pending" for i in candidates),
        "review_decisions": db.execute("SELECT COUNT(*) FROM word_correction_reviews").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='word_correction_reviews'").fetchone() else 0,
    }


def overview(db_path):
    with get_db(db_path) as db:
        events = db.execute("SELECT COUNT(*), MAX(ts) FROM radio_events").fetchone()
        from services.telegram_sync import ensure_tables
        ensure_tables(db)
        # Виправлені оператором фрази (а не правила й не голоси).
        corrections = db.execute("SELECT COUNT(*) FROM (SELECT source_id FROM word_correction_votes UNION SELECT fix_id FROM correction_sources)").fetchone()[0]
        total_rules = db.execute("SELECT COUNT(*) FROM word_corrections").fetchone()[0]
        learning = learning_overview(db)
        dictionaries = dict(db.execute("SELECT dictionary_type, COUNT(*) FROM dictionary_entries WHERE enabled=1 GROUP BY dictionary_type"))
    return {"events": events[0], "last_event_at": events[1], "active_rules": learning["active_rules"],
            "corrections": corrections, "total_rules": total_rules, "learning": learning,
            "dictionary_entries": sum(dictionaries.values()), "dictionaries": dictionaries}


def automatic_changes(diagnostics_path):
    """Лише числа з діагностики приймача: зміни Qwen і застосування правил."""
    try:
        with open(diagnostics_path, encoding="utf-8") as source:
            data = json.load(source)
        counters = data.get("counters") or {}
    except (OSError, ValueError, AttributeError):
        return {"available": False}
    number = lambda key: counters.get(key) if type(counters.get(key)) is int else 0
    updated = data.get("updated_at") if isinstance(data.get("updated_at"), (int, float)) else None
    # Приймач пише знімок кожні ~30 с; старіший за 2 хв — це вже минулий запуск.
    fresh = updated is not None and 0 <= time.time() - updated < 120
    return {"available": True, "fresh": fresh, "updated_at": updated,
            "started_at": data.get("started_at") if isinstance(data.get("started_at"), (int, float)) else None,
            "qwen_changed": number("llm.changed"), "qwen_accepted": number("llm.accepted"),
            "qwen_rejected": number("llm.rejected_dictionary") + number("llm.rejected_rewrite"),
            "rules_applied": number("learned.applied")}
