from services.sqlite_connection import get_db
from services.word_store import (
    init_word_db,
    save_word_feedback,
    load_active_word_rules,
)


def test_votes_activate_and_deactivate(tmp_path):
    db_path = tmp_path / "learning.db"
    init_word_db(db_path)

    save_word_feedback(
        db_path, "db:1",
        "На ган 110", "Наган 110",
    )

    assert load_active_word_rules(db_path) == []

    save_word_feedback(
        db_path, "db:2",
        "На ган 112", "Наган 112",
    )

    assert load_active_word_rules(db_path) == [
        ("на ган", "наган")
    ]

    # Отменяем исправление второй записи.
    save_word_feedback(
        db_path, "db:2",
        "На ган 112", "На ган 112",
    )

    assert load_active_word_rules(db_path) == []


def test_conflict_suspends_and_restores_rule(tmp_path):
    db_path = tmp_path / "learning.db"
    init_word_db(db_path)

    save_word_feedback(
        db_path, "db:1",
        "Лира 68", "Ліра 68",
    )

    save_word_feedback(
        db_path, "db:2",
        "Лира 70", "Ліра 70",
    )

    assert load_active_word_rules(db_path) == [
        ("лира", "ліра")
    ]

    # Третья запись подтверждает другой вариант.
    save_word_feedback(
        db_path, "db:3",
        "Лира 72", "Ліро 72",
    )

    assert load_active_word_rules(db_path) == []

    with get_db(db_path) as db:
        statuses = set(db.execute("""
            SELECT corrected, status
            FROM word_corrections
            WHERE original = 'лира'
        """).fetchall())

    assert statuses == {
        ("ліра", "pending"),
        ("ліро", "pending"),
    }

    # Оператор отменяет конфликтующее исправление.
    save_word_feedback(
        db_path, "db:3",
        "Лира 72", "Лира 72",
    )

    assert load_active_word_rules(db_path) == [
        ("лира", "ліра")
    ]
