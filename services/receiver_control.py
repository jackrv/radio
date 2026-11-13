"""Команды паузы приёмника, общие для API и radio.py."""

import math
import time

from services.sqlite_connection import get_db


MAX_PAUSE_MINUTES = 240


def ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS receiver_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            pause_until REAL NOT NULL
        )
    """)


def read_pause_state(db_path, *, now=None):
    """Читает запрос на паузу. Истечение таймера означает работу."""

    now = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        ensure_table(db)

        row = db.execute(
            "SELECT pause_until FROM receiver_control WHERE id = 1"
        ).fetchone()

    until = float(row[0]) if row is not None else 0.0
    active = until > now

    return {
        "pause_requested": active,
        "resume_at": until if active else None,
        "remaining_seconds": max(
            0, math.ceil(until - now)
        ) if active else 0,
    }


def set_pause_minutes(db_path, minutes, *, now=None):
    """
    0: возобновить приём.
    1..240: запросить паузу на указанное число минут.
    """

    if type(minutes) is not int or not (
        0 <= minutes <= MAX_PAUSE_MINUTES
    ):
        raise ValueError("Пауза должна быть от 0 до 240 минут")

    now = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        ensure_table(db)

        if minutes == 0:
            db.execute(
                "DELETE FROM receiver_control WHERE id = 1"
            )
        else:
            until = now + minutes * 60

            db.execute("""
                INSERT INTO receiver_control (id, pause_until)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pause_until = excluded.pause_until
            """, (until,))

    return read_pause_state(db_path, now=now)
