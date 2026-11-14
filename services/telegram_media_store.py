"""Telegram references and deletion tombstones. Never stores audio bytes."""

import re
from contextlib import nullcontext
from urllib.parse import urlsplit, parse_qs

from services.sqlite_connection import get_db

FIX_RE = re.compile(r"(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{8,32})\Z")


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_phrase_messages (
        fix_id TEXT PRIMARY KEY, chat_id TEXT, message_id INTEGER, voice_file_id TEXT,
        state TEXT NOT NULL DEFAULT 'visible' CHECK(state IN ('visible','deleting','deleted'))
    )""")


def is_hidden(db_path, fix_id, *, db=None):
    with (get_db(db_path) if db is None else nullcontext(db)) as conn:
        ensure_table(conn)
        row = conn.execute("SELECT state FROM telegram_phrase_messages WHERE fix_id=?", (fix_id,)).fetchone()
        return bool(row and row[0] != "visible")


def remember_message(db_path, fix_id, message):
    if not isinstance(fix_id, str) or not FIX_RE.fullmatch(fix_id) or not isinstance(message, dict):
        return
    msg_id = message.get("message_id")
    chat_id = (message.get("chat") or {}).get("id")
    file_id = (message.get("voice") or {}).get("file_id")
    if type(msg_id) is not int or chat_id is None:
        return
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute("""INSERT INTO telegram_phrase_messages (fix_id,chat_id,message_id,voice_file_id)
            VALUES (?,?,?,?) ON CONFLICT(fix_id) DO UPDATE SET
            chat_id=excluded.chat_id, message_id=excluded.message_id,
            voice_file_id=COALESCE(excluded.voice_file_id, telegram_phrase_messages.voice_file_id)
        """, (fix_id, str(chat_id), msg_id, file_id if isinstance(file_id, str) else None))


def callback_fix_id(db_path, data, message):
    candidates = [data[4:]] if data.startswith("del:") else []
    for row in (message.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            value = button.get("callback_data", "")
            if value.startswith("fix_"):
                candidates.append(value[4:])
            url = (button.get("web_app") or {}).get("url", "")
            candidates.extend(parse_qs(urlsplit(url).query).get("fix_id", []))
    for candidate in candidates:
        if FIX_RE.fullmatch(candidate):
            return candidate
    with get_db(db_path) as db:
        ensure_table(db)
        row = db.execute("SELECT fix_id FROM telegram_phrase_messages WHERE chat_id=? AND message_id=?",
                         (str((message.get("chat") or {}).get("id")), message.get("message_id"))).fetchone()
    return row[0] if row else None


def deletion_intent(db_path, fix_id):
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute("""INSERT INTO telegram_phrase_messages (fix_id,state) VALUES (?,'deleting')
            ON CONFLICT(fix_id) DO UPDATE SET state=CASE
            WHEN state='deleted' THEN 'deleted' ELSE 'deleting' END""", (fix_id,))


def deletion_result(db_path, fix_id, success):
    with get_db(db_path) as db:
        ensure_table(db)
        if success:
            db.execute("UPDATE telegram_phrase_messages SET state='deleted',voice_file_id=NULL WHERE fix_id=?", (fix_id,))
        else:
            db.execute("UPDATE telegram_phrase_messages SET state='visible' WHERE fix_id=? AND state='deleting'", (fix_id,))


def audio_file_id(db_path, fix_id):
    with get_db(db_path) as db:
        ensure_table(db)
        row = db.execute("SELECT voice_file_id FROM telegram_phrase_messages WHERE fix_id=? AND state='visible'", (fix_id,)).fetchone()
    if not row or not row[0]:
        raise LookupError("Аудиозапись недоступна")
    return row[0]


def has_audio(db_path, fix_id):
    try:
        audio_file_id(db_path, fix_id)
        return True
    except LookupError:
        return False
