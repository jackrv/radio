import logging
import threading
from unittest.mock import Mock

import pytest

from services.bot_state import BotState
from services.notify_mode_store import (
    load_notify_mode,
    save_notify_mode,
)
from services.telegram import TelegramService


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Відносний db_path сервісу ніколи не створює файл у корені проєкту."""
    monkeypatch.chdir(tmp_path)


def test_off_is_persistent(tmp_path):
    db_path = tmp_path / "radio.db"

    save_notify_mode(db_path, "off")

    assert load_notify_mode(db_path) == "off"

    state = BotState()
    state.enable_persistence(db_path)

    assert state.get_notify_mode() == "off"


def test_bot_state_sees_external_change(tmp_path):
    db_path = tmp_path / "radio.db"

    state = BotState()
    state.enable_persistence(db_path)

    assert state.get_notify_mode() == "all"

    save_notify_mode(db_path, "off")
    assert state.get_notify_mode() == "off"

    save_notify_mode(db_path, "watch")
    assert state.get_notify_mode() == "watch"


def make_service(mode):
    lock = threading.Lock()

    service = TelegramService(
        bot_token="test-token",
        chat_id="123",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test_silent_mode"),
        state_lock=lock,
        get_notify_mode=lambda: mode["value"],
        set_notify_mode=lambda value: mode.update(value=value),
    )

    return service


def test_off_blocks_new_notifications():
    mode = {"value": "off"}
    service = make_service(mode)
    service.send_voice = Mock()

    service.notify(
        "Эфир", "", "", "Проверка", b"\x00\x00"
    )

    assert service._outgoing_queue.empty()
    service.send_voice.assert_not_called()


def test_off_drops_queued_notifications():
    mode = {"value": "all"}
    service = make_service(mode)

    started = threading.Event()
    release = threading.Event()
    sent = []

    def fake_send(pcm, caption, fix_id):
        if not started.is_set():
            started.set()
            release.wait(timeout=5)
        sent.append(caption)

    service.send_voice = fake_send

    try:
        service.notify(
            "Эфир", "", "", "Первое", b"\x00\x00"
        )

        assert started.wait(timeout=3)

        service.notify(
            "Эфир", "", "", "Второе", b"\x00\x00"
        )

        mode["value"] = "off"

    finally:
        release.set()

    service._outgoing_queue.join()

    assert len(sent) == 1
    assert "Первое" in sent[0]


def test_off_command():
    mode = {"value": "all"}
    service = make_service(mode)
    service.send_text = Mock()

    service.handle_update({
        "message": {
            "chat": {"id": 123},
            "text": "/off",
        }
    })

    assert mode["value"] == "off"
    service.send_text.assert_called_once()
