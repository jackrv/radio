import logging
import threading
from unittest.mock import patch

import pytest

from services.telegram import TelegramService
from services.event_store import init_db
from services.sqlite_connection import get_db


@pytest.fixture
def setup_service(tmp_path):
    path = tmp_path / "telegram_learning.db"
    init_db(path)

    service = TelegramService(
        bot_token="test-token",
        chat_id="123",
        sample_rate=16000,
        db_path=str(path),
        log=logging.getLogger("test_word_learning"),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )

    service.send_text = lambda *args, **kwargs: None
    return service, path


def correct_message(service, fix_id, text):
    callback = {
        "callback_query": {
            "id": "test-callback",
            "data": f"fix_{fix_id}",
            "message": {
                "message_id": 1,
                "chat": {"id": 123},
            },
        }
    }

    message = {
        "message": {
            "chat": {"id": 123},
            "text": text,
        }
    }

    with patch("services.telegram.requests.post"):
        service.handle_update(callback)
        service.handle_update(message)


def get_rules(path):
    with get_db(path) as db:
        return db.execute("""
            SELECT original, corrected, confirmations
            FROM word_corrections
            ORDER BY original, corrected
        """).fetchall()


def test_mem_messages_accumulate_confirmations(setup_service):
    service, path = setup_service

    first = service.register_phrase_for_fix(
        "На ган 110", None
    )

    second = service.register_phrase_for_fix(
        "На ган 112", None
    )

    correct_message(service, first, "Наган 110")
    correct_message(service, second, "Наган 112")

    assert get_rules(path) == [
        ("на ган", "наган", 2)
    ]


def test_repeated_mem_correction_not_counted_twice(setup_service):
    service, path = setup_service

    fix_id = service.register_phrase_for_fix(
        "Лира 68", None
    )

    correct_message(service, fix_id, "Ліра 68")
    correct_message(service, fix_id, "Ліра 68")

    assert get_rules(path) == [
        ("лира", "ліра", 1)
    ]


def test_revising_mem_correction_replaces_vote(setup_service):
    service, path = setup_service

    fix_id = service.register_phrase_for_fix(
        "Лира 68", None
    )

    correct_message(service, fix_id, "Ліра 68")
    correct_message(service, fix_id, "Ліро 68")

    assert get_rules(path) == [
        ("лира", "ліра", 0),
        ("лира", "ліро", 1),
    ]
