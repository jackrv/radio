import pytest

from services.word_store import (
    init_word_db,
    save_word_feedback,
)
from services.sqlite_connection import get_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "learning.db"
    init_word_db(path)
    return path


def get_rules(path):
    with get_db(path) as db:
        return db.execute("""
            SELECT original, corrected, confirmations, status
            FROM word_corrections
            ORDER BY original, corrected
        """).fetchall()


def test_different_records_accumulate(db_path):
    save_word_feedback(
        db_path, "db:1", "На бат 110", "Набат 110"
    )
    save_word_feedback(
        db_path, "db:2", "На бат 112", "Набат 112"
    )

    assert get_rules(db_path) == [
        ("на бат", "набат", 2, "active")
    ]


def test_same_record_does_not_count_twice(db_path):
    for _ in range(3):
        save_word_feedback(
            db_path, "db:1",
            "Лина 68", "Ліна 68"
        )

    assert get_rules(db_path) == [
        ("лина", "ліна", 1, "pending")
    ]


def test_revising_record_changes_vote(db_path):
    save_word_feedback(
        db_path, "db:1", "Лина 68", "Ліна 68"
    )

    save_word_feedback(
        db_path, "db:1", "Лина 68", "Ліно 68"
    )

    assert get_rules(db_path) == [
        ("лина", "ліна", 0, "pending"),
        ("лина", "ліно", 1, "pending"),
    ]


def test_removed_correction_removes_vote(db_path):
    save_word_feedback(
        db_path, "db:1", "Лина 68", "Ліна 68"
    )

    save_word_feedback(
        db_path, "db:1", "Лина 68", "Лина 68"
    )

    assert get_rules(db_path) == [
        ("лина", "ліна", 0, "pending")
    ]


def test_rules_remain_pending_and_disabled_preserved(db_path):
    save_word_feedback(
        db_path, "db:1", "На бат 110", "Набат 110"
    )

    with get_db(db_path) as db:
        db.execute("""
            UPDATE word_corrections
            SET status = 'disabled'
            WHERE original = 'на бат'
        """)

    save_word_feedback(
        db_path, "db:2", "На бат 112", "Набат 112"
    )

    assert get_rules(db_path) == [
        ("на бат", "набат", 2, "disabled")
    ]
