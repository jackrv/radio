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
        waiting_for_correction={},
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
        "Связь с Telegram восстановлена "
        "после %s ошибок опроса",
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
