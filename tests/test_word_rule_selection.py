import pytest

from services.sqlite_connection import get_db
from services.word_store import (
    init_word_db,
    load_active_word_rules,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "rules.db"
    init_word_db(path)
    return path


def add_rule(path, original, corrected, count, status):
    with get_db(path) as db:
        db.execute("""
            INSERT INTO word_corrections
                (original, corrected, confirmations, status)
            VALUES (?, ?, ?, ?)
        """, (original, corrected, count, status))


def test_only_confirmed_active_rules(db_path):
    add_rule(db_path, "на бат", "набат", 3, "active")
    add_rule(db_path, "калиниця", "калина", 1, "active")
    add_rule(db_path, "лина", "ліна", 5, "pending")
    add_rule(db_path, "хвилия", "хвиля", 4, "disabled")

    assert load_active_word_rules(db_path) == [
        ("на бат", "набат")
    ]


def test_pending_conflict_blocks_activation(db_path):
    add_rule(db_path, "лина", "ліна", 3, "active")
    add_rule(db_path, "лина", "лино", 1, "pending")

    assert load_active_word_rules(db_path) == []


def test_disabled_conflict_does_not_block(db_path):
    add_rule(db_path, "лина", "ліна", 3, "active")
    add_rule(db_path, "лина", "лино", 2, "disabled")

    assert load_active_word_rules(db_path) == [
        ("лина", "ліна")
    ]


def test_both_active_conflicting_rules_are_blocked(db_path):
    add_rule(db_path, "лина", "ліна", 3, "active")
    add_rule(db_path, "лина", "лино", 3, "active")

    assert load_active_word_rules(db_path) == []
