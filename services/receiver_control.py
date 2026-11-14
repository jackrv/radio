"""Команды паузы, общие для API и radio.py."""

import math
import time

from services.sqlite_connection import get_db


MAX_PAUSE_MINUTES = 240


class PauseVersionConflict(Exception):
    """Команда основана на устаревшем состоянии."""


def ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS receiver_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            pause_until REAL NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS receiver_control_revision (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            revision INTEGER NOT NULL
        )
    """)


def _read_state(db, now):
    until, revision = db.execute("""
        SELECT
            COALESCE(
                (SELECT pause_until FROM receiver_control WHERE id=1),
                0
            ),
            COALESCE(
                (SELECT revision FROM receiver_control_revision WHERE id=1),
                0
            )
    """).fetchone()

    until = float(until)
    active = until > now

    return {
        "pause_requested": active,
        "resume_at": until if active else None,
        "remaining_seconds": (
            max(0, math.ceil(until - now)) if active else 0
        ),
        "revision": int(revision),
    }


def read_pause_state(db_path, *, now=None):
    now = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        ensure_table(db)
        return _read_state(db, now)


def set_pause_minutes(
    db_path,
    minutes,
    *,
    now=None,
    expected_revision=None,
):
    if type(minutes) is not int or not (
        0 <= minutes <= MAX_PAUSE_MINUTES
    ):
        raise ValueError("Пауза должна быть от 0 до 240 минут")

    if expected_revision is not None and (
        type(expected_revision) is not int
        or expected_revision < 0
    ):
        raise ValueError("Некорректная версия команды")

    now = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        # Сериализует конкурирующие команды.
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)

        current = db.execute("""
            SELECT COALESCE(
                (SELECT revision
                 FROM receiver_control_revision WHERE id=1),
                0
            )
        """).fetchone()[0]

        if (
            expected_revision is not None
            and expected_revision != current
        ):
            raise PauseVersionConflict(
                "Состояние приёмника изменилось"
            )

        if minutes == 0:
            db.execute(
                "DELETE FROM receiver_control WHERE id=1"
            )
        else:
            db.execute("""
                INSERT INTO receiver_control (id, pause_until)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pause_until=excluded.pause_until
            """, (now + minutes * 60,))

        db.execute("""
            INSERT INTO receiver_control_revision (id, revision)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                revision=excluded.revision
        """, (current + 1,))

        return _read_state(db, now)
