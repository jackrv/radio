"""Temporary radio phrases for Telegram Mini App (up to 200, 24 hours)."""

import re
import time
from contextlib import nullcontext

from services.sqlite_connection import get_db

RETENTION_SECONDS = 24 * 60 * 60
MAX_PHRASES = 200
TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")


def _check_token(token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ValueError("Некорректный токен фразы")


def ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS telegram_recent_phrases (
            token TEXT PRIMARY KEY,
            original_text TEXT NOT NULL,
            current_text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)


def register_phrase(db_path, token, text, *, max_phrases=200):
    _check_token(token)
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise ValueError("Некорректный текст фразы")
    if type(max_phrases) is not int or not 1 <= max_phrases <= MAX_PHRASES:
        raise ValueError("Некорректный лимит фраз")

    now = int(time.time())
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        db.execute("""
            INSERT INTO telegram_recent_phrases
                (token, original_text, current_text, created_at)
            VALUES (?, ?, ?, ?)
        """, (token, text, text, now))
        db.execute(
            "DELETE FROM telegram_recent_phrases WHERE created_at <= ?",
            (now - RETENTION_SECONDS,),
        )
        db.execute("""
            DELETE FROM telegram_recent_phrases
            WHERE token NOT IN (
                SELECT token FROM telegram_recent_phrases
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            )
        """, (max_phrases,))


def load_phrase(db_path, token, *, db=None):
    _check_token(token)
    manager = get_db(db_path) if db is None else nullcontext(db)
    with manager as conn:
        ensure_table(conn)
        cutoff = int(time.time()) - RETENTION_SECONDS
        # Clean all expired rows even if no new phrases arrive.
        conn.execute(
            "DELETE FROM telegram_recent_phrases WHERE created_at <= ?",
            (cutoff,),
        )
        row = conn.execute("""
            SELECT original_text, current_text, created_at
            FROM telegram_recent_phrases
            WHERE token = ?
        """, (token,)).fetchone()
    if row is None:
        return None
    return {"original_text": row[0], "current_text": row[1]}


def update_phrase_text(db, token, corrected_text, *, allow_missing=False):
    _check_token(token)
    ensure_table(db)
    cursor = db.execute("""
        UPDATE telegram_recent_phrases
        SET current_text = ?
        WHERE token = ? AND created_at > ?
    """, (corrected_text, token, int(time.time()) - RETENTION_SECONDS))
    if cursor.rowcount != 1 and not allow_missing:
        raise LookupError("Фраза не найдена или срок хранения истёк")
    return cursor.rowcount == 1
