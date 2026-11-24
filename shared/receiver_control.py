"""Команды паузы, общие для API и приёмника."""

import math
import time

from shared.sqlite_connection import get_db


MAX_PAUSE_MINUTES = 60


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
        raise ValueError(f"Пауза має бути від 0 до {MAX_PAUSE_MINUTES} хвилин")

    if expected_revision is not None and (
        type(expected_revision) is not int
        or expected_revision < 0
    ):
        raise ValueError("Некоректна версія команди")

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
                "Стан приймача змінився"
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


# ---------- Утримання на час прослуховування ----------
# Окремо від паузи оператора: інша мета, власний короткий термін і без версії.
# Термін завжди обмежений, тож закрита вкладка не залишить приймач вимкненим.
MAX_LISTENING_HOLD_S = 120.0


def ensure_hold_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS receiver_listening_hold (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            hold_until REAL NOT NULL
        )
    """)


def set_listening_hold(db_path, seconds, *, now=None):
    """Утримувати мікрофон закритим ще `seconds` секунд (0 — відпустити зараз)."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= MAX_LISTENING_HOLD_S:
        raise ValueError(f"Утримання має бути від 0 до {MAX_LISTENING_HOLD_S:g} с")
    now = time.time() if now is None else float(now)
    with get_db(db_path) as db:
        ensure_hold_table(db)
        db.execute("""INSERT INTO receiver_listening_hold (id, hold_until) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET hold_until = excluded.hold_until""", (now + seconds,))
    return {"active": seconds > 0, "hold_until": now + seconds}


def reception_request(db_path, *, now=None):
    """Що просять від приймача: "pause" (оператор), "listening" (прослуховування) або None."""
    now = time.time() if now is None else float(now)
    with get_db(db_path) as db:
        ensure_table(db)
        ensure_hold_table(db)
        if _read_state(db, now)["pause_requested"]:
            return "pause"
        row = db.execute("SELECT hold_until FROM receiver_listening_hold WHERE id = 1").fetchone()
    return "listening" if row and float(row[0]) > now else None
