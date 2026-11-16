"""Стійкі адреси копій Telegram і черга оновлення виправлених фраз.

HTTP ніколи не виконується у транзакції виправлення. Повторне збереження
оновлює ревізію; підтвердження старої ревізії не губить новішу правку.
"""

import time

import requests

from core.diagnostics import runtime
from services.sqlite_connection import get_db
from services.telegram_media_store import FIX_RE, is_hidden
from services.telegram_phrase_store import load_phrase


def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS correction_sources (
        fix_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_deliveries (
        fix_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('voice','text')),
        applied_revision INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, unavailable INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(fix_id,chat_id,message_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS telegram_deliveries_due ON telegram_deliveries(next_try)")


def migrate_legacy(db_path):
    """Одноразове читання давніх адрес під час запуску обслуговування."""
    with get_db(db_path) as db:
        ensure_tables(db)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_phrase_messages'").fetchone():
            db.execute("""INSERT OR IGNORE INTO telegram_deliveries(fix_id,chat_id,message_id,kind)
                SELECT fix_id,CAST(chat_id AS INTEGER),message_id,
                    CASE WHEN voice_file_id IS NULL THEN 'text' ELSE 'voice' END
                FROM telegram_phrase_messages WHERE state='visible' AND CAST(chat_id AS INTEGER)>0 AND message_id IS NOT NULL""")


def queue_correction(db, fix_id):
    ensure_tables(db)
    db.execute("""INSERT INTO correction_sources(fix_id,revision,updated_at) VALUES (?,1,?)
        ON CONFLICT(fix_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at""",
               (fix_id, time.time()))
    # A new revision retries even if a previous request exhausted its retries.
    db.execute("UPDATE telegram_deliveries SET attempts=0,next_try=0 WHERE fix_id=?", (fix_id,))


def remember_delivery(db_path, fix_id, message, kind):
    if not isinstance(fix_id, str) or not FIX_RE.fullmatch(fix_id) or not isinstance(message, dict):
        return
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if type(chat_id) is not int or type(message_id) is not int or chat_id <= 0:
        return
    with get_db(db_path) as db:
        ensure_tables(db)
        # Always begin at zero: an edit could commit during sendVoice's HTTP request.
        db.execute("INSERT OR IGNORE INTO telegram_deliveries(fix_id,chat_id,message_id,kind) VALUES (?,?,?,?)",
                   (fix_id, chat_id, message_id, kind))


def forget_delivery(db_path, chat_id, message_id):
    with get_db(db_path) as db:
        ensure_tables(db)
        db.execute("UPDATE telegram_deliveries SET unavailable=1 WHERE chat_id=? AND message_id=?",
                   (chat_id, message_id))


def _current_text(db_path, fix_id):
    if is_hidden(db_path, fix_id):
        return None
    if fix_id.startswith("mem:"):
        phrase = load_phrase(db_path, fix_id[4:])
        return phrase["current_text"] if phrase else None
    with get_db(db_path) as db:
        row = db.execute("SELECT text FROM radio_events WHERE id=?", (int(fix_id[3:]),)).fetchone()
    return row[0] if row else None


_HEADER_CACHE = {}


def _dictionaries(db_path):
    """Поточні словники з SQLite; кеш за відбитком рядків і поточними алиасами."""
    import callsigns as callsign_module
    from callsigns import Callsigns, command_dictionary
    from services.dictionary_loader import dictionary_fingerprint
    with get_db(db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone():
            return None
        key = (str(db_path), dictionary_fingerprint(db), id(callsign_module.ALIASES))
        if key in _HEADER_CACHE:
            return _HEADER_CACHE[key]
        lines = {}
        for kind, line in db.execute("""SELECT dictionary_type, raw_line FROM dictionary_entries
                WHERE enabled = 1 ORDER BY dictionary_type, line_number"""):
            lines.setdefault(kind, []).append(line)
    result = (*(Callsigns.from_lines(lines.get(kind, [])) for kind in ("callsigns", "telegram")),
              command_dictionary(lines.get("commands", [])))
    _HEADER_CACHE.clear()
    _HEADER_CACHE[key] = result
    return result


def corrected_caption(db_path, text):
    """Шапка перераховується з виправленого тексту за поточними словниками, як при надсиланні."""
    from core.analyzer import fmt, fmt_spoken, split_roles
    from services.telegram_outbox import build_caption
    try:
        dictionaries = _dictionaries(db_path)
    except Exception:
        dictionaries = None
    if dictionaries is None:
        return "✏️ Виправлено оператором\n\n" + text
    callsigns, watch, commands = dictionaries
    to_found, from_found = split_roles(text, callsigns)
    return "✏️ Виправлено оператором\n" + build_caption(fmt(watch.find(text)) or "Ефір", fmt(to_found), fmt(from_found),
                                                       text, fmt_spoken(commands.find(text)))


def clipped(text, units):
    """Telegram рахує UTF-16, а Python — символи Unicode."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= units * 2:
        return text
    return encoded[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def sync_corrections(service, *, limit=5, now=None):
    now = time.time() if now is None else now
    if not getattr(service,"_legacy_imported",False):
        migrate_legacy(service.db_path)
        service._legacy_imported = True
    with get_db(service.db_path) as db:
        ensure_tables(db)
        # Expired mem references must not grow forever or resurrect old phrases.
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_recent_phrases'").fetchone()
        if exists:
            db.execute("""DELETE FROM telegram_deliveries WHERE fix_id LIKE 'mem:%' AND NOT EXISTS
                (SELECT 1 FROM telegram_recent_phrases p WHERE 'mem:'||p.token=fix_id AND created_at>?)""", (now-86400,))
        jobs = db.execute("""SELECT d.fix_id,d.chat_id,d.message_id,d.kind,c.revision
            FROM telegram_deliveries d JOIN correction_sources c ON c.fix_id=d.fix_id
            WHERE d.unavailable=0 AND d.applied_revision<c.revision AND d.next_try<=? AND d.attempts<8
            ORDER BY d.next_try,d.rowid LIMIT ?""", (now, limit)).fetchall()
    for fix_id, chat, message, kind, revision in jobs:
        if getattr(service,"_maintenance_stop",None) is not None and service._maintenance_stop.is_set():
            break
        # Recheck access at send time. No raw phrase in logs or jobs.
        state = service._user_state(chat)
        if state is None:
            # A temporary SQLite failure is indistinguishable from revoked access
            # at this boundary. Fail closed, but do not discard a pending edit.
            with get_db(service.db_path) as db:
                db.execute("UPDATE telegram_deliveries SET next_try=? WHERE fix_id=? AND chat_id=? AND message_id=?",
                           (now+30,fix_id,chat,message))
            continue
        text = _current_text(service.db_path, fix_id)
        if text is None:
            forget_delivery(service.db_path, chat, message)
            continue
        field = "caption" if kind == "voice" else "text"
        payload = {"chat_id": chat, "message_id": message,
                   field: clipped(corrected_caption(service.db_path, text), 1024 if kind == "voice" else 4096),
                   "reply_markup": service.keyboard_for(state[0], fix_id)}
        # Reserve before HTTP. A crash/timeout cannot cause a hot retry loop.
        with get_db(service.db_path) as db:
            db.execute("""UPDATE telegram_deliveries SET next_try=?,attempts=attempts+1
                WHERE fix_id=? AND chat_id=? AND message_id=?""", (now+30,fix_id,chat,message))
        try:
            response = service._request("post", "editMessageCaption" if kind == "voice" else "editMessageText",
                                        data=payload, timeout=(5, 10))
        except requests.RequestException:
            runtime.count("telegram.correction_edit_deferred")
            service.log.warning("Оновлення виправленої копії Telegram відкладено")
            continue
        try:
            result = response.json()
            if not isinstance(result, dict): result = {}
        except (ValueError, AttributeError):
            result = {}
        ok = result.get("ok") is True or str(result.get("description", "")).startswith("Bad Request: message is not modified")
        gone = result.get("description") in ("Bad Request: message to edit not found", "Bad Request: message can't be edited") or response.status_code == 403
        with get_db(service.db_path) as db:
            if ok:
                runtime.count("telegram.correction_edit_confirmed")
                db.execute("""UPDATE telegram_deliveries SET applied_revision=MAX(applied_revision,?),attempts=0,next_try=0
                    WHERE fix_id=? AND chat_id=? AND message_id=?""", (revision,fix_id,chat,message))
            else:
                runtime.count("telegram.correction_edit_unavailable" if gone else "telegram.correction_edit_deferred")
                retry = result.get("parameters", {}).get("retry_after", 30)
                retry = max(5, min(3600, retry)) if type(retry) is int else 30
                db.execute("""UPDATE telegram_deliveries SET unavailable=?,next_try=?
                    WHERE fix_id=? AND chat_id=? AND message_id=?""", (int(gone),now+retry,fix_id,chat,message))
    return len(jobs)


def sync_status(db_path):
    with get_db(db_path) as db:
        ensure_tables(db)
        row = db.execute("""SELECT
            COUNT(*),
            COALESCE(SUM(d.unavailable=0 AND d.applied_revision<c.revision AND d.attempts<8),0),
            COALESCE(SUM(d.unavailable=0 AND d.applied_revision<c.revision AND d.attempts>=8),0)
            FROM telegram_deliveries d LEFT JOIN correction_sources c ON c.fix_id=d.fix_id""").fetchone()
    return {"copies":row[0],"pending":row[1],"exhausted":row[2]}
