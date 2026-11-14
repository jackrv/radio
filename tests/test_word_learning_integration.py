import sqlite3

import pytest

from database import init_db as init_dictionary_db
from services.event_store import (
    init_db as init_event_db,
    save_event,
    save_correction,
)
from services.sqlite_connection import get_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "integration.db"
    init_dictionary_db(path)
    init_event_db(path)
    return path


def create_event(db_path, raw_text):
    return save_event(
        "", "", "", "",
        raw_text,
        db_path=db_path,
        raw_text=raw_text,
    )


def test_word_learning_from_different_events(db_path):
    event1 = create_event(db_path, "На ган 110")
    event2 = create_event(db_path, "На ган 112")

    save_correction(
        "На ган 110", "Наган 110",
        event1, db_path,
    )

    # Повторное исправление той же записи.
    save_correction(
        "На ган 110", "Наган 110",
        event1, db_path,
    )

    save_correction(
        "На ган 112", "Наган 112",
        event2, db_path,
    )

    with get_db(db_path) as db:
        result = db.execute("""
            SELECT original, corrected,
                   confirmations, status
            FROM word_corrections
        """).fetchall()

    assert result == [
        ("на ган", "наган", 2, "active")
    ]


def test_word_learning_rollback(db_path):
    event_id = create_event(db_path, "На ган 110")

    with get_db(db_path) as db:
        db.execute("""
            CREATE TRIGGER reject_word_vote
            BEFORE INSERT ON word_correction_votes
            BEGIN
                SELECT RAISE(ABORT, 'test failure');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError):
        save_correction(
            "На ган 110", "Наган 110",
            event_id, db_path,
        )

    with get_db(db_path) as db:
        text = db.execute("""
            SELECT text FROM radio_events WHERE id = ?
        """, (event_id,)).fetchone()[0]

        votes = db.execute("""
            SELECT COUNT(*) FROM word_correction_votes
        """).fetchone()[0]

    assert text == "На ган 110"
    assert votes == 0


def test_memory_correction_without_event(db_path):
    save_correction(
        "На ган 110",
        "Наган 110",
        db_path=db_path,
        source_id="mem:abc123",
    )

    with get_db(db_path) as db:
        votes = db.execute("""
            SELECT source_id, original, corrected
            FROM word_correction_votes
        """).fetchall()

    assert votes == [
        ("mem:abc123", "на ган", "наган"),
    ]
