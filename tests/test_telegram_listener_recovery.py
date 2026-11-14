import threading
from unittest.mock import Mock, patch

import pytest
import requests

from services.telegram import TelegramService
from services.telegram_retry import listener_retry_delay


def make_service():
    return TelegramService(
        bot_token="test_token",
        chat_id="123",
        sample_rate=16000,
        db_path=":memory:",
        log=Mock(),
        state_lock=threading.Lock(),
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )


def successful_response():
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"result": []}
    return response


def test_listener_retry_delays():
    assert [
        listener_retry_delay(n) for n in range(1, 7)
    ] == [5, 10, 20, 40, 60, 60]


def test_listener_recovers_after_timeouts():
    service = make_service()
    delays = []

    with (
        patch(
            "services.telegram.requests.get",
            side_effect=[
                requests.Timeout("network unavailable"),
                requests.Timeout("network unavailable"),
                successful_response(),
                KeyboardInterrupt(),
            ],
        ),
        patch("services.telegram.time.sleep",
              side_effect=delays.append),
    ):
        with pytest.raises(KeyboardInterrupt):
            service.callback_listener()

    assert delays == [5, 10]
    service.log.info.assert_called_once_with(
        "Зв’язок із Telegram відновлено "
        "після %s помилок опитування",
        2,
    )


def test_listener_recovers_after_http_error():
    service = make_service()
    delays = []

    failed_response = Mock()
    failed_response.status_code = 503

    with (
        patch(
            "services.telegram.requests.get",
            side_effect=[
                failed_response,
                successful_response(),
                KeyboardInterrupt(),
            ],
        ),
        patch("services.telegram.time.sleep",
              side_effect=delays.append),
    ):
        with pytest.raises(KeyboardInterrupt):
            service.callback_listener()

    assert delays == [5]
    service.log.info.assert_called_once()


def test_listener_survives_sqlite_error_when_saving_offset():
    import sqlite3

    service = make_service()
    service._updates_offset = 10
    handled = []
    service.handle_update = handled.append
    update = Mock(status_code=200)
    update.json.return_value = {"result": [{"update_id": 10, "message": {}}]}
    saved = []

    def save(db_path, offset):
        if not saved:
            saved.append("failed")
            raise sqlite3.OperationalError("database is locked")
        saved.append(offset)

    delays = []
    with (
        patch("services.telegram.requests.get", side_effect=[update, update, KeyboardInterrupt()]),
        patch("services.telegram.save_telegram_offset", side_effect=save),
        patch("services.telegram.time.sleep", side_effect=delays.append),
    ):
        with pytest.raises(KeyboardInterrupt):
            service.callback_listener()

    assert len(handled) == 2, "update оброблено повторно після збою SQLite"
    assert saved == ["failed", 11]
    assert service._updates_offset == 11
    assert delays == [5]
