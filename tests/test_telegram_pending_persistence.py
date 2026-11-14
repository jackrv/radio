import sqlite3
import threading
from unittest.mock import Mock, patch

import pytest

from database import init_db as init_dictionary_db
from services.event_store import (
    init_db as init_event_db,
    save_event,
)
from services.telegram import TelegramService
from services.telegram_pending import load_pending_correction
from services.telegram_offset_store import load_telegram_offset


def make_service(db):
    service = TelegramService(
        bot_token="test",
        chat_id="123",
        sample_rate=16000,
        db_path=str(db),
        log=Mock(),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )
    service.send_text = Mock()
    return service


def callback(update_id, fix_id):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "data": f"fix_{fix_id}",
            "message": {
                "chat": {"id": 123},
                "message_id": 1,
            },
        },
    }


def message(update_id, text):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 123},
            "text": text,
        },
    }


def test_pending_survives_restart(tmp_path):
    db = tmp_path / "radio.db"
    init_event_db(db)

    first = make_service(db)
    fix_id = first.register_phrase_for_fix("Лира 68", None)

    with patch("services.telegram.requests.post"):
        first.handle_update(callback(100, fix_id))

    assert load_pending_correction(db, "123")["raw_text"] == "Лира 68"

    restarted = make_service(db)
    assert restarted.waiting_for_correction == {}

    restarted.handle_update(message(101, "Ліра 68"))

    assert load_pending_correction(db, "123") is None
    assert load_telegram_offset(db) == 102

    with sqlite3.connect(db) as conn:
        votes = conn.execute("""
            SELECT COUNT(*) FROM word_correction_votes
        """).fetchone()[0]

    assert votes == 1


def test_duplicate_message_does_not_add_vote(tmp_path):
    db = tmp_path / "radio.db"
    init_event_db(db)

    service = make_service(db)
    fix_id = service.register_phrase_for_fix("Лира 68", None)

    with patch("services.telegram.requests.post"):
        service.handle_update(callback(100, fix_id))

    correction = message(101, "Ліра 68")
    service.handle_update(correction)
    service.handle_update(correction)

    with sqlite3.connect(db) as conn:
        votes = conn.execute("""
            SELECT COUNT(*) FROM word_correction_votes
        """).fetchone()[0]

    assert votes == 1
    assert load_telegram_offset(db) == 102
    assert service.send_text.call_count == 2


def test_cancel_survives_restart(tmp_path):
    db = tmp_path / "radio.db"
    init_event_db(db)

    service = make_service(db)
    fix_id = service.register_phrase_for_fix("Лира 68", None)

    with patch("services.telegram.requests.post"):
        service.handle_update(callback(100, fix_id))
        service.handle_update({
            "update_id": 101,
            "callback_query": {
                "id": "cancel",
                "data": "cancel_fix",
                "message": {
                    "chat": {"id": 123},
                    "message_id": 1,
                },
            },
        })

    assert load_pending_correction(db, "123") is None

    restarted = make_service(db)
    restarted.handle_update(message(102, "Ліра 68"))

    restarted.send_text.assert_not_called()


def test_failed_offset_rolls_back_correction(tmp_path):
    db = tmp_path / "radio.db"
    init_event_db(db)

    service = make_service(db)
    fix_id = service.register_phrase_for_fix("Лира 68", None)

    with patch("services.telegram.requests.post"):
        service.handle_update(callback(100, fix_id))

    assert load_telegram_offset(db) is None

    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER fail_offset
            BEFORE INSERT ON telegram_poll_state
            BEGIN
                SELECT RAISE(ABORT, 'simulated failure');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError):
        service.handle_update(message(101, "Ліра 68"))

    assert load_pending_correction(db, "123") is not None
    assert load_telegram_offset(db) is None

    with sqlite3.connect(db) as conn:
        votes = conn.execute("""
            SELECT COUNT(*) FROM word_correction_votes
        """).fetchone()[0]

    assert votes == 0


def test_event_correction_survives_restart(tmp_path):
    db = tmp_path / "radio.db"
    init_dictionary_db(db)
    init_event_db(db)

    event_id = save_event(
        "", "", "", "", "Лира 68",
        db_path=db,
        raw_text="Лира 68",
    )

    first = make_service(db)

    with patch("services.telegram.requests.post"):
        first.handle_update(callback(100, f"db:{event_id}"))

    restarted = make_service(db)
    restarted.handle_update(message(101, "Ліра 68"))

    with sqlite3.connect(db) as conn:
        text = conn.execute(
            "SELECT text FROM radio_events WHERE id=?",
            (event_id,),
        ).fetchone()[0]

    assert text == "Ліра 68"
    assert load_telegram_offset(db) == 102
