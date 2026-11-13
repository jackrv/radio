import pytest

from services.sqlite_connection import get_db
from services.word_store import (
    init_word_db,
    load_active_word_rules,
    refresh_word_rule_statuses,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "activation.db"
    init_word_db(path)
    return path


def add_rule(db, original, corrected, count, status="pending"):
    db.execute("""
        INSERT INTO word_corrections
            (original, corrected, confirmations, status)
        VALUES (?, ?, ?, ?)
    """, (original, corrected, count, status))


def get_statuses(db):
    return db.execute("""
        SELECT original, corrected, confirmations, status
        FROM word_corrections
        ORDER BY original, corrected
    """).fetchall()


def test_two_confirmations_activate_rule(db_path):
    with get_db(db_path) as db:
        add_rule(db, "на ган", "наган", 2)
        refresh_word_rule_statuses(db)

        assert get_statuses(db) == [
            ("на ган", "наган", 2, "active")
        ]

    assert load_active_word_rules(db_path) == [
        ("на ган", "наган")
    ]


def test_one_confirmation_stays_pending(db_path):
    with get_db(db_path) as db:
        add_rule(db, "лира", "ліра", 1)
        refresh_word_rule_statuses(db)

        assert get_statuses(db) == [
            ("лира", "ліра", 1, "pending")
        ]


def test_conflict_suspends_active_rule(db_path):
    with get_db(db_path) as db:
        add_rule(db, "лира", "ліра", 3, "active")
        add_rule(db, "лира", "лиро", 1)

        refresh_word_rule_statuses(db)

        assert set(get_statuses(db)) == {
            ("лира", "ліра", 3, "pending"),
            ("лира", "лиро", 1, "pending"),
        }

    assert load_active_word_rules(db_path) == []


def test_disabled_rule_is_not_reactivated(db_path):
    with get_db(db_path) as db:
        add_rule(db, "лира", "ліра", 4, "disabled")
        add_rule(db, "лира", "лиро", 2)

        refresh_word_rule_statuses(db)

        assert get_statuses(db) == [
            ("лира", "лиро", 2, "active"),
            ("лира", "ліра", 4, "disabled"),
        ]


def test_rule_deactivates_when_votes_decrease(db_path):
    with get_db(db_path) as db:
        add_rule(db, "на ган", "наган", 2, "active")

        db.execute("""
            UPDATE word_corrections
            SET confirmations = 1
            WHERE original = 'на ган'
        """)

        refresh_word_rule_statuses(db)

        assert get_statuses(db) == [
            ("на ган", "наган", 1, "pending")
        ]
