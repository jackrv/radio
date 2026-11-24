import sqlite3

from shared.event_store import init_db, save_event


def test_migration_preserves_old_events(tmp_path):
    path = str(tmp_path / "old.db")

    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE radio_events (
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
            INSERT INTO radio_events
                (callsigns, commands, recipient, sender, text)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "вишня 700", "", "", "",
            "Вишня 700, прийом"
        ))

    init_db(path)
    init_db(path)  # Повторная миграция безопасна.

    with sqlite3.connect(path) as db:
        rows = db.execute("""
            SELECT callsigns, text, raw_text
            FROM radio_events
        """).fetchall()

    assert rows == [
        ("вишня 700", "Вишня 700, прийом", None)
    ]


def test_save_event_with_original_whisper_text(tmp_path):
    path = str(tmp_path / "new.db")
    init_db(path)

    event_id = save_event(
        "вишня 700",
        "",
        "",
        "",
        "Вишня 700, прийом",
        path,
        raw_text="Вішня 700, прийом",
    )

    with sqlite3.connect(path) as db:
        row = db.execute("""
            SELECT text, raw_text
            FROM radio_events
            WHERE id = ?
        """, (event_id,)).fetchone()

    assert row == (
        "Вишня 700, прийом",
        "Вішня 700, прийом",
    )
