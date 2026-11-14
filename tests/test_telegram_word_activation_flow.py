import sqlite3
import threading
from unittest.mock import Mock

from callsigns import Callsigns
from database import init_db as init_dictionary_db
from services.event_store import (
    init_db as init_event_db,
    save_event,
)
from services.telegram import TelegramService
from core.word_rule_engine import apply_verified_word_rules


def test_two_telegram_corrections_activate_word_rule(
    tmp_path, monkeypatch
):
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
            ("callsigns", 1, "липка: 700 720"),
        )

    callsigns = Callsigns.from_lines([
        "липка: 700 720"
    ])
    empty = Callsigns.from_lines([])
    dictionaries = (callsigns, empty, empty)

    # Запрещаем реальные сетевые запросы.
    monkeypatch.setattr(
        "services.telegram.requests.post",
        Mock(),
    )

    pending = {}

    service = TelegramService(
        bot_token="test_token",
        chat_id="123",
        sample_rate=16000,
        db_path=db_path,
        log=Mock(),
        state_lock=threading.Lock(),
        waiting_for_correction=pending,
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )

    service.send_text = Mock()

    def correct_through_telegram(original, corrected, number):
        event_id = save_event(
            "", "", "", "",
            original,
            db_path=db_path,
            raw_text=original,
        )

        # Имитируем нажатие кнопки исправления.
        service.handle_update({
            "callback_query": {
                "id": f"callback_{number}",
                "data": f"fix_db:{event_id}",
                "message": {
                    "chat": {"id": 123},
                    "message_id": 100 + number,
                },
            },
        })

        assert pending["123"]["event_id"] == event_id
        assert pending["123"]["raw_text"] == original

        # Имитируем отправку исправленного текста.
        service.handle_update({
            "message": {
                "chat": {"id": 123},
                "text": corrected,
            },
        })

        assert "123" not in pending
        return event_id

    # Первое независимое исправление.
    first_id = correct_through_telegram(
        "Улыбка 700",
        "Липка 700",
        1,
    )

    # Одного подтверждения пока недостаточно.
    assert apply_verified_word_rules(
        "Улыбка 700",
        db_path,
        dictionaries,
    ) == "Улыбка 700"

    # Второе независимое исправление.
    second_id = correct_through_telegram(
        "Улыбка 720",
        "Липка 720",
        2,
    )

    # Проверяем автоматическую активацию.
    assert apply_verified_word_rules(
        "Улыбка 700, на связи",
        db_path,
        dictionaries,
    ) == "Липка 700, на связи"

    assert apply_verified_word_rules(
        "Улыбка 720",
        db_path,
        dictionaries,
    ) == "Липка 720"

    # Недопустимый номер не исправляем.
    assert apply_verified_word_rules(
        "Улыбка 999",
        db_path,
        dictionaries,
    ) == "Улыбка 999"

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
        ("улыбка", "липка", 2, "active")
    ]

    assert votes == [
        (f"db:{first_id}",),
        (f"db:{second_id}",),
    ]

    assert events == [
        ("Улыбка 700", "Липка 700"),
        ("Улыбка 720", "Липка 720"),
    ]

    # Старая таблица больше не пополняется.
