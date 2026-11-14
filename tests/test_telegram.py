import json
import logging
import threading

import pytest

from services.telegram import TelegramService


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Відносний db_path сервісу ніколи не створює файл у корені проєкту."""
    monkeypatch.chdir(tmp_path)


def make_service():
    return TelegramService(
        bot_token="",
        chat_id="",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test"),
        state_lock=threading.Lock(),
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )


def test_keyboard_without_fix_id():
    service = make_service()

    keyboard = json.loads(service.get_keyboard())

    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "del"
    assert len(keyboard["inline_keyboard"][0]) == 1


def test_keyboard_with_fix_id():
    service = make_service()

    rows = json.loads(service.get_keyboard("db:12345"))["inline_keyboard"]

    assert rows[0] == [{"text": "🗑 Видалити", "callback_data": "del:db:12345"}]
    assert rows[1][0]["web_app"]["url"].endswith("?fix_id=db%3A12345")
    assert len(rows) == 2


def test_register_phrase_ids():
    service = make_service()

    assert service.register_phrase_for_fix("фраза", 42) == "db:42"
    # Без Telegram сповіщень немає, тож тимчасова фраза не потрібна.
    assert service.register_phrase_for_fix("фраза", None) is None


def test_notification_modes():
    modes = {"value": "all"}

    service = TelegramService(
        bot_token="",
        chat_id="123",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test"),
        state_lock=threading.Lock(),
        get_notify_mode=lambda: modes["value"],
        set_notify_mode=lambda mode: modes.update(value=mode),
    )

    service.send_text = lambda text, reply_markup=None, parse_mode="HTML": None

    for command, expected in [
        ("/watch", "watch"),
        ("/commands", "commands"),
        ("/all", "all"),
    ]:
        service.handle_update({
            "message": {
                "chat": {"id": 123},
                "text": command,
            }
        })
        assert modes["value"] == expected


def test_foreign_chat_is_ignored():
    from unittest.mock import Mock

    modes = {"value": "all"}
    service = make_service()
    service.chat_id = "123"
    service.set_notify_mode = lambda mode: modes.update(value=mode)
    service.send_text = Mock()

    service.handle_update({
        "message": {
            "chat": {"id": 456},
            "text": "/watch",
        }
    })

    assert modes["value"] == "all"
    service.send_text.assert_not_called()
