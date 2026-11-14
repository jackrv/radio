import requests
import time
import json
from unittest.mock import Mock

import pytest

from services.telegram import TelegramService


def make_service(tmp_path):
    return TelegramService(
        bot_token="test_token",
        sample_rate=16000,
        db_path=str(tmp_path / "events.db"),
        log=Mock(),
    )


def response_for(*updates):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"result": list(updates)}
    return response


def test_temporary_failure_retries_without_advancing_offset(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    update = {"update_id": 41, "message": {"text": "test"}}
    response = response_for(update)

    service.handle_update = Mock(
        side_effect=[ValueError("temporary"), None]
    )

    mock_get = Mock(
        side_effect=[response, response, KeyboardInterrupt()]
    )

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    offsets = [
        call.kwargs["params"]["offset"]
        for call in mock_get.call_args_list
    ]

    assert offsets == [None, None, 42]
    assert service.handle_update.call_count == 2
    assert not (tmp_path / "telegram_failed_updates.jsonl").exists()


def test_poison_update_is_saved_and_next_update_processed(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)

    bad = {"update_id": 51, "message": {"text": "bad"}}
    good = {"update_id": 52, "message": {"text": "good"}}
    response = response_for(bad, good)

    def handle(update):
        if update["update_id"] == 51:
            raise ValueError("broken update")

    service.handle_update = Mock(side_effect=handle)

    mock_get = Mock(
        side_effect=[
            response,
            response,
            response,
            KeyboardInterrupt(),
        ]
    )

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    assert service._updates_offset == 53
    assert service.handle_update.call_count == 4

    path = tmp_path / "telegram_failed_updates.jsonl"
    assert path.exists()

    saved = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert saved == [bad]


def test_failed_quarantine_does_not_advance_offset(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    bad = {"update_id": 61, "message": {"text": "bad"}}
    response = response_for(bad)

    service.handle_update = Mock(
        side_effect=RuntimeError("processing failed")
    )

    service._quarantine_update = Mock(
        side_effect=OSError("disk full")
    )

    mock_get = Mock(
        side_effect=[
            response,
            response,
            response,
            KeyboardInterrupt(),
        ]
    )

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    assert service._updates_offset is None
    assert service.handle_update.call_count == 3
    service._quarantine_update.assert_called_once_with(bad)
