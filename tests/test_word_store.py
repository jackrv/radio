import sqlite3

import pytest

from services.word_store import init_word_db
from services.sqlite_connection import get_db


def test_creates_word_corrections_table(tmp_path):
    path = tmp_path / "test.db"
    init_word_db(path)

    with get_db(path) as db:
        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(word_corrections)"
            )
        }

    assert {
        "original",
        "corrected",
        "confirmations",
        "status",
    }.issubset(columns)


def test_defaults_and_idempotent_initialization(tmp_path):
    path = tmp_path / "test.db"
    init_word_db(path)

    with get_db(path) as db:
        db.execute("""
            INSERT INTO word_corrections
                (original, corrected)
            VALUES ('на ган', 'наган')
        """)

    init_word_db(path)

    with get_db(path) as db:
        rows = db.execute("""
            SELECT original, corrected,
                   confirmations, status
            FROM word_corrections
        """).fetchall()

    assert rows == [
        ("на ган", "наган", 0, "pending")
    ]


def test_conflicting_variants_can_be_stored(tmp_path):
    path = tmp_path / "test.db"
    init_word_db(path)

    with get_db(path) as db:
        db.execute("""
            INSERT INTO word_corrections
                (original, corrected)
            VALUES ('лира', 'ліра')
        """)
        db.execute("""
            INSERT INTO word_corrections
                (original, corrected)
            VALUES ('лира', 'лираа')
        """)

    with get_db(path) as db:
        count = db.execute("""
            SELECT COUNT(*)
            FROM word_corrections
            WHERE original = 'лира'
        """).fetchone()[0]

    assert count == 2


def test_rejects_invalid_data(tmp_path):
    path = tmp_path / "test.db"
    init_word_db(path)

    with pytest.raises(sqlite3.IntegrityError):
        with get_db(path) as db:
            db.execute("""
                INSERT INTO word_corrections
                    (original, corrected, confirmations)
                VALUES ('лира', 'ліра', -1)
            """)

    with pytest.raises(sqlite3.IntegrityError):
        with get_db(path) as db:
            db.execute("""
                INSERT INTO word_corrections
                    (original, corrected, status)
                VALUES ('на ган', 'наган', 'unknown')
            """)
