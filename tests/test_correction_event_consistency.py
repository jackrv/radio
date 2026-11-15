import sqlite3

import pytest

from database import init_db as init_dictionary_db

from services.event_store import (
    init_db,
    save_event,
    save_correction,
)


ORIGINAL = "Вішня 700, я Кобза 110, прийом"
CORRECTED = "Вишня 700, я Кобза 112, прийом"


@pytest.fixture
def event_db(tmp_path):
    path = str(tmp_path / "events.db")

    init_db(path)
    init_dictionary_db(path)

    with sqlite3.connect(path) as db:
        db.executemany(
            """
            INSERT INTO dictionary_entries
                (dictionary_type, line_number, raw_line)
            VALUES (?, ?, ?)
            """,
            [
                ("callsigns", 1, "вишня: 700"),
                ("callsigns", 2, "кобза: 110 112"),
                ("commands", 1, "прийом"),
            ],
        )

    event_id = save_event(
        "вишня 700, кобза 110",
        "прийом",
        "вишня 700",
        "кобза 110",
        ORIGINAL,
        path,
        raw_text=ORIGINAL,
    )

    return path, event_id


def test_correction_recalculates_all_fields(event_db):
    path, event_id = event_db

    save_correction(
        ORIGINAL,
        CORRECTED,
        event_id,
        path,
    )

    with sqlite3.connect(path) as db:
        row = db.execute(
            """
            SELECT text, callsigns, commands,
                   recipient, sender, raw_text
            FROM radio_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

    assert row == (
        CORRECTED,
        "вишня 700, кобза 112",
        "прийом",
        "вишня 700",
        "кобза 112",
        ORIGINAL,
    )


def test_failed_update_rolls_back_training(event_db):
    path, event_id = event_db

    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TRIGGER reject_event_update
            BEFORE UPDATE ON radio_events
            BEGIN
                SELECT RAISE(ABORT, 'update rejected');
            END
        """)

    with pytest.raises(sqlite3.DatabaseError):
        save_correction(
            ORIGINAL,
            CORRECTED,
            event_id,
            path,
        )

    with sqlite3.connect(path) as db:
        text = db.execute(
            "SELECT text FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()[0]

        count = db.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0]

    assert text == ORIGINAL
    assert count == 0


def test_memory_only_training_still_works(tmp_path):
    path = str(tmp_path / "memory.db")
    init_db(path)

    save_correction(
        "Кобзя 110",
        "Кобза 110",
        db_path=path,
        source_id="mem:test-1",
    )

    with sqlite3.connect(path) as db:
        votes = db.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0]

    assert votes == 1

def test_missing_event_does_not_save_training(event_db):
    path, _ = event_db

    with pytest.raises(LookupError):
        save_correction(
            ORIGINAL,
            CORRECTED,
            event_id=999999,
            db_path=path,
        )

    with sqlite3.connect(path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0]

    assert count == 0
