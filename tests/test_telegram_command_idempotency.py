import sqlite3
from unittest.mock import Mock

import pytest

from services.bot_state import BotState
from services.telegram import TelegramService
from services.telegram_offset_store import (
    load_telegram_offset,
)


def make_service(tmp_path):
    db = tmp_path / "radio.db"
    state = BotState()
    state.enable_persistence(db)

    service = TelegramService(
        bot_token="test",
        chat_id="123",
        sample_rate=16000,
        db_path=str(db),
        log=Mock(),
        state_lock=state.lock,
        waiting_for_correction=state.waiting_for_correction,
        get_notify_mode=state.get_notify_mode,
        set_notify_mode=state.set_notify_mode,
        apply_telegram_mode=state.apply_telegram_mode,
    )
    service.send_text = Mock()
    return db, state, service


def command(update_id, text):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 123},
            "text": text,
        },
    }


def test_mode_and_offset_are_saved_together(tmp_path):
    db, state, service = make_service(tmp_path)

    service.handle_update(command(100, "/watch"))

    assert state.get_notify_mode() == "watch"
    assert load_telegram_offset(db) == 101
    service.send_text.assert_called_once()


def test_duplicate_command_is_ignored(tmp_path):
    db, state, service = make_service(tmp_path)

    update = command(100, "/watch")

    service.handle_update(update)
    service.handle_update(update)

    assert state.get_notify_mode() == "watch"
    assert load_telegram_offset(db) == 101
    service.send_text.assert_called_once()


def test_older_command_cannot_overwrite_newer(tmp_path):
    db, state, service = make_service(tmp_path)

    service.handle_update(command(100, "/watch"))
    service.handle_update(command(102, "/off"))
    service.handle_update(command(101, "/all"))

    assert state.get_notify_mode() == "off"
    assert load_telegram_offset(db) == 103
    assert service.send_text.call_count == 2


def test_failed_transaction_rolls_back_mode(tmp_path):
    db, state, service = make_service(tmp_path)

    # Создаём таблицу offset до установки триггера.
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
        service.handle_update(command(100, "/watch"))

    assert state.get_notify_mode() == "all"
    assert load_telegram_offset(db) is None
    service.send_text.assert_not_called()
