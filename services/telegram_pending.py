"""Незавершённые исправления Telegram в SQLite."""

import re

from services.sqlite_connection import get_db
from services.event_store import save_correction
from services.telegram_phrase_store import update_phrase_text
from services.telegram_offset_store import (
    _ensure_table as ensure_offset_table,
    save_telegram_offset,
)


def ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS telegram_pending_corrections (
            chat_id TEXT PRIMARY KEY,
            raw_text TEXT NOT NULL,
            event_id INTEGER,
            source_id TEXT
        )
    """)


def save_pending_correction(db_path, chat_id, pending):
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute("""
            INSERT INTO telegram_pending_corrections
                (chat_id, raw_text, event_id, source_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                raw_text = excluded.raw_text,
                event_id = excluded.event_id,
                source_id = excluded.source_id
        """, (
            chat_id,
            pending["raw_text"],
            pending["event_id"],
            pending.get("source_id"),
        ))


def load_pending_correction(db_path, chat_id):
    with get_db(db_path) as db:
        ensure_table(db)
        row = db.execute("""
            SELECT raw_text, event_id, source_id
            FROM telegram_pending_corrections
            WHERE chat_id = ?
        """, (chat_id,)).fetchone()

    if row is None:
        return None

    return {
        "raw_text": row[0],
        "event_id": row[1],
        "source_id": row[2],
    }


def cancel_pending_correction(db_path, chat_id):
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute("""
            DELETE FROM telegram_pending_corrections
            WHERE chat_id = ?
        """, (chat_id,))


def apply_pending_correction(
    db_path, chat_id, corrected_text, update_id
):
    """Атомарно сохраняет исправление, удаляет ожидание
    и подтверждает Telegram update_id.
    """

    if type(update_id) is not int or update_id < 0:
        raise ValueError("Некоректний update_id")

    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")

        ensure_table(db)
        ensure_offset_table(db)

        offset_row = db.execute("""
            SELECT next_offset
            FROM telegram_poll_state
            WHERE id = 1
        """).fetchone()

        if offset_row and update_id < offset_row[0]:
            return None

        row = db.execute("""
            SELECT raw_text, event_id, source_id
            FROM telegram_pending_corrections
            WHERE chat_id = ?
        """, (chat_id,)).fetchone()

        if row is None:
            return None

        pending = {
            "raw_text": row[0],
            "event_id": row[1],
            "source_id": row[2],
        }

        options = {}
        if pending["source_id"] is not None:
            options["source_id"] = pending["source_id"]

        save_correction(
            pending["raw_text"],
            corrected_text,
            pending["event_id"],
            db_path,
            db=db,
            **options,
        )

        source = pending.get("source_id")
        if source and re.fullmatch(r"mem:[0-9a-f]{32}", source):
            # The pending chat correction must stay usable even when a
            # recent-phrase entry has already expired or was pruned.
            update_phrase_text(
                db, source[4:], corrected_text, allow_missing=True
            )

        db.execute("""
            DELETE FROM telegram_pending_corrections
            WHERE chat_id = ?
        """, (chat_id,))

        save_telegram_offset(
            db_path,
            update_id + 1,
            db=db,
        )

        return pending
