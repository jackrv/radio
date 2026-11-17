import sqlite3

from callsigns import Callsigns
from database import init_db as init_dictionary_db
from services.event_store import (
    init_db as init_event_db,
    save_event,
)
from services.miniapp_correction_editor import save_edit_draft
from services.word_rule_engine import apply_verified_word_rules


def test_two_editor_corrections_activate_word_rule(tmp_path):
    db_path = str(tmp_path / "telegram_learning.db")

    init_dictionary_db(db_path)
    init_event_db(db_path)

    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO dictionary_entries
                (dictionary_type, line_number, raw_line)
            VALUES (?, ?, ?)
            """,
            ("callsigns", 1, "калина: 700 720"),
        )

    callsigns = Callsigns.from_lines([
        "калина: 700 720"
    ])
    empty = Callsigns.from_lines([])
    dictionaries = (callsigns, empty, empty)

    def correct_through_editor(original, corrected, number):
        event_id = save_event(
            "", "", "", "",
            original,
            db_path=db_path,
            raw_text=original,
        )
        # Виправлення через редактор Mini App.
        save_edit_draft(db_path, f"db:{event_id}", original, corrected)
        return event_id

    # Первое независимое исправление.
    first_id = correct_through_editor(
        "Калиниця 700",
        "Калина 700",
        1,
    )

    # Одного подтверждения пока недостаточно.
    assert apply_verified_word_rules(
        "Калиниця 700",
        db_path,
        dictionaries,
    ) == "Калиниця 700"

    # Второе независимое исправление.
    second_id = correct_through_editor(
        "Калиниця 720",
        "Калина 720",
        2,
    )

    # Проверяем автоматическую активацию.
    assert apply_verified_word_rules(
        "Калиниця 700, на связи",
        db_path,
        dictionaries,
    ) == "Калина 700, на связи"

    assert apply_verified_word_rules(
        "Калиниця 720",
        db_path,
        dictionaries,
    ) == "Калина 720"

    # Недопустимый номер не исправляем.
    assert apply_verified_word_rules(
        "Калиниця 999",
        db_path,
        dictionaries,
    ) == "Калиниця 999"

    with sqlite3.connect(db_path) as db:
        rules = db.execute("""
            SELECT original, corrected,
                   confirmations, status
            FROM word_corrections
        """).fetchall()

        votes = db.execute("""
            SELECT source_id
            FROM word_correction_votes
            ORDER BY source_id
        """).fetchall()

        events = db.execute("""
            SELECT raw_text, text
            FROM radio_events
            ORDER BY id
        """).fetchall()

    assert rules == [
        ("калиниця", "калина", 2, "active")
    ]

    assert votes == [
        (f"db:{first_id}",),
        (f"db:{second_id}",),
    ]

    assert events == [
        ("Калиниця 700", "Калина 700"),
        ("Калиниця 720", "Калина 720"),
    ]

    # Старая таблица больше не пополняется.
