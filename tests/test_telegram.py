import json
import logging
from unittest.mock import Mock

import pytest

from services.telegram import TelegramService
from services.user_notify_store import load_user_mode
from services.user_store import bootstrap_admin, init_users_db


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Відносний db_path сервісу ніколи не створює файл у корені проєкту."""
    monkeypatch.chdir(tmp_path)


def make_service():
    return TelegramService(
        bot_token="",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test"),
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
    service = make_service()
    init_users_db(service.db_path)
    bootstrap_admin(service.db_path, 123)
    service.send_text = Mock()

    for command, expected in [
        ("/watch", "watch"),
        ("/commands", "commands"),
        ("/all", "all"),
    ]:
        service.handle_update({
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "text": command,
            }
        })
        assert load_user_mode(service.db_path, 123) == ("admin", expected)
        assert service.send_text.call_args.kwargs["chat_id"] == 123


def test_foreign_chat_is_ignored():
    service = make_service()
    service.send_text = Mock()

    service.handle_update({
        "message": {
            "chat": {"id": 456, "type": "private"},
            "from": {"id": 456},
            "text": "/watch",
        }
    })

    assert load_user_mode(service.db_path, 456) is None
    service.send_text.assert_not_called()
