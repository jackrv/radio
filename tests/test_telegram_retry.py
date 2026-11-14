import logging

import requests

from services.telegram import TelegramService
from services.telegram_retry import retry_delay
from tests.test_telegram_outbox import RECIPIENTS


def test_exponential_backoff():
    error = requests.ConnectionError("offline")

    assert retry_delay(1, error) == 2
    assert retry_delay(2, error) == 4
    assert retry_delay(3, error) == 8


def test_telegram_429_retry_after():
    response = requests.Response()
    response.status_code = 429
    response._content = b'{"parameters":{"retry_after":15}}'

    error = requests.HTTPError(response=response)

    assert retry_delay(1, error) == 15
    assert retry_delay(2, error) == 15


def test_send_voice_retries_after_429(monkeypatch):
    service = TelegramService(
        bot_token="test-token",
        sample_rate=16000,
        db_path=":memory:",
        log=logging.getLogger("test_telegram_retry"),
    )
    service._user_state = lambda user_id: ("admin", "all")

    monkeypatch.setattr(
        service, "pcm_to_ogg", lambda pcm: b"fake-ogg"
    )

    calls = []
    delays = []

    def fake_post(url, **kwargs):
        calls.append(url)
        assert kwargs["timeout"] == (10, 60)

        response = requests.Response()
        response.url = url

        if len(calls) == 1:
            response.status_code = 429
            response._content = (
                b'{"parameters":{"retry_after":7}}'
            )
        else:
            response.status_code = 200
            response._content = b'{"ok":true}'

        return response

    monkeypatch.setattr(
        "requests.post", fake_post
    )
    monkeypatch.setattr(
        "time.sleep", delays.append
    )

    service.send_voice(b"\x00\x00", "Test", None, RECIPIENTS)

    assert len(calls) == 2
    assert delays == [7]
