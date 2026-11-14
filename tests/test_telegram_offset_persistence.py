import requests
import time
import json
import sqlite3
from unittest.mock import Mock

import pytest

from services.telegram import TelegramService
import services.telegram_updates as telegram_updates
from services.telegram_offset_store import (
    load_telegram_offset,
    save_telegram_offset,
)


def make_service(db_path):
    return TelegramService(
        bot_token="test_token",
        sample_rate=16000,
        db_path=str(db_path),
        log=Mock(),
    )


def response_for(*updates):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"result": list(updates)}
    return response


def test_offset_store_survives_reopen_and_never_decreases(tmp_path):
    db = tmp_path / "radio.db"

    assert load_telegram_offset(db) is None

    save_telegram_offset(db, 42)
    save_telegram_offset(db, 40)

    assert load_telegram_offset(db) == 42

    save_telegram_offset(db, 53)
    assert load_telegram_offset(db) == 53


def test_new_service_resumes_from_persisted_offset(
    tmp_path, monkeypatch
):
    db = tmp_path / "radio.db"

    first = make_service(db)
    first.handle_update = Mock()

    old = {"update_id": 41, "message": {"text": "old"}}

    mock_get = Mock(side_effect=[
        response_for(old),
        KeyboardInterrupt(),
    ])

    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(KeyboardInterrupt):
        first.callback_listener()

    assert load_telegram_offset(db) == 42

    second = make_service(db)
    second.handle_update = Mock()

    new = {"update_id": 42, "message": {"text": "new"}}

    mock_get = Mock(side_effect=[
        response_for(old, new),
        KeyboardInterrupt(),
    ])

    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(KeyboardInterrupt):
        second.callback_listener()

    assert mock_get.call_args_list[0].kwargs[
        "params"
    ]["offset"] == 42

    second.handle_update.assert_called_once_with(new)
    assert load_telegram_offset(db) == 43


def test_quarantined_update_offset_survives_restart(
    tmp_path, monkeypatch
):
    db = tmp_path / "radio.db"
    service = make_service(db)

    bad = {"update_id": 51, "message": {"text": "bad"}}
    service.handle_update = Mock(
        side_effect=RuntimeError("broken")
    )

    mock_get = Mock(side_effect=[
        response_for(bad),
        response_for(bad),
        response_for(bad),
        KeyboardInterrupt(),
    ])

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    assert load_telegram_offset(db) == 52

    quarantine = tmp_path / "telegram_failed_updates.jsonl"
    saved = [
        json.loads(line)
        for line in quarantine.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert saved == [bad]

    restarted = make_service(db)
    mock_get = Mock(side_effect=[KeyboardInterrupt()])
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(KeyboardInterrupt):
        restarted.callback_listener()

    assert mock_get.call_args.kwargs["params"]["offset"] == 52


def test_failed_offset_save_does_not_acknowledge(
    tmp_path, monkeypatch
):
    db = tmp_path / "radio.db"
    service = make_service(db)

    update = {"update_id": 80, "message": {"text": "test"}}
    service.handle_update = Mock()

    monkeypatch.setattr(
        requests,
        "get",
        Mock(side_effect=[response_for(update), KeyboardInterrupt()]),
    )

    def fail_save(path, offset):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(
        telegram_updates,
        "save_telegram_offset",
        fail_save,
    )
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)

    # Слухач не падає: пауза й повтор, але update не підтверджено.
    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    assert delays == [5]
    assert service._updates_offset is None
    assert load_telegram_offset(db) is None
