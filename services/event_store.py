import sqlite3
from contextlib import contextmanager

from rapidfuzz import fuzz, process


@contextmanager
def get_db(db_path="events.db"):
    conn = sqlite3.connect(db_path, timeout=15.0)

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path="events.db"):
    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS radio_events (
                id INTEGER PRIMARY KEY,
                ts TEXT DEFAULT CURRENT_TIMESTAMP,
                callsigns TEXT,
                commands TEXT,
                recipient TEXT,
                sender TEXT,
                text TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY,
                original TEXT UNIQUE,
                corrected TEXT
            )
        """)

        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(radio_events)")
        }

        for col in ("recipient", "sender"):
            if col not in cols:
                db.execute(
                    f"ALTER TABLE radio_events ADD COLUMN {col} TEXT"
                )


def apply_learned_corrections(
    text: str,
    db_path="events.db",
    fuzzy_score=86,
) -> tuple[str, bool]:
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT original, corrected FROM corrections"
        ).fetchall()

    if not rows:
        return text, False

    from callsigns import normalize

    norm_text = normalize(text)

    for original, corrected in rows:
        if normalize(original) == norm_text:
            return corrected, True

    mapping = {
        normalize(original): corrected
        for original, corrected in rows
        if normalize(original)
    }

    if not mapping:
        return text, False

    best = process.extractOne(
        norm_text,
        list(mapping.keys()),
        scorer=fuzz.ratio,
    )

    if best and best[1] >= fuzzy_score:
        return mapping[best[0]], True

    return text, False


def resolve_event_text(event_id: int, db_path="events.db"):
    with get_db(db_path) as db:
        row = db.execute(
            "SELECT text FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()

    return row[0] if row else None


def save_correction(
    original: str,
    corrected: str,
    event_id: int | None = None,
    db_path="events.db",
):
    with get_db(db_path) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO corrections (original, corrected)
            VALUES (?, ?)
            """,
            (original, corrected),
        )

        if event_id is not None:
            db.execute(
                "UPDATE radio_events SET text = ? WHERE id = ?",
                (corrected, event_id),
            )


def save_event(
    callsigns: str,
    commands: str,
    recipient: str,
    sender: str,
    text: str,
    db_path="events.db",
) -> int:
    with get_db(db_path) as db:
        cursor = db.execute(
            """
            INSERT INTO radio_events
                (callsigns, commands, recipient, sender, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (callsigns, commands, recipient, sender, text),
        )
        return cursor.lastrowid
