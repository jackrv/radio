from contextlib import nullcontext
"""Позиция обработки Telegram getUpdates между перезапусками."""

from services.sqlite_connection import get_db


def _ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS telegram_poll_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_offset INTEGER NOT NULL
        )
    """)


def load_telegram_offset(db_path):
    with get_db(db_path) as db:
        _ensure_table(db)
        row = db.execute("""
            SELECT next_offset
            FROM telegram_poll_state
            WHERE id = 1
        """).fetchone()

    return int(row[0]) if row else None


def save_telegram_offset(db_path, next_offset, *, db=None):
    if type(next_offset) is not int or next_offset < 0:
        raise ValueError("Некоректний Telegram offset")

    manager = (
        get_db(db_path) if db is None
        else nullcontext(db)
    )

    with manager as db:
        _ensure_table(db)

        db.execute("""
            INSERT INTO telegram_poll_state (id, next_offset)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                next_offset = MAX(
                    telegram_poll_state.next_offset,
                    excluded.next_offset
                )
        """, (next_offset,))
