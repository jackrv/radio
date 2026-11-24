from shared.sqlite_connection import get_db
from shared.word_store import (
    init_word_db,
    save_word_feedback,
    load_active_word_rules,
)


def test_votes_activate_and_deactivate(tmp_path):
    db_path = tmp_path / "learning.db"
    init_word_db(db_path)

    save_word_feedback(
        db_path, "db:1",
        "На бат 110", "Набат 110",
    )

    assert load_active_word_rules(db_path) == []

    save_word_feedback(
        db_path, "db:2",
        "На бат 112", "Набат 112",
    )

    assert load_active_word_rules(db_path) == [
        ("на бат", "набат")
    ]

    # Отменяем исправление второй записи.
    save_word_feedback(
        db_path, "db:2",
        "На бат 112", "На бат 112",
    )

    assert load_active_word_rules(db_path) == []


def test_conflict_suspends_and_restores_rule(tmp_path):
    db_path = tmp_path / "learning.db"
    init_word_db(db_path)

    save_word_feedback(
        db_path, "db:1",
        "Лина 68", "Ліна 68",
    )

    save_word_feedback(
        db_path, "db:2",
        "Лина 70", "Ліна 70",
    )

    assert load_active_word_rules(db_path) == [
        ("лина", "ліна")
    ]

    # Третья запись подтверждает другой вариант.
    save_word_feedback(
        db_path, "db:3",
        "Лина 72", "Ліно 72",
    )

    assert load_active_word_rules(db_path) == []

    with get_db(db_path) as db:
        statuses = set(db.execute("""
            SELECT corrected, status
            FROM word_corrections
            WHERE original = 'лина'
        """).fetchall())

    assert statuses == {
        ("ліна", "pending"),
        ("ліно", "pending"),
    }

    # Оператор отменяет конфликтующее исправление.
    save_word_feedback(
        db_path, "db:3",
        "Лина 72", "Лина 72",
    )

    assert load_active_word_rules(db_path) == [
        ("лина", "ліна")
    ]
