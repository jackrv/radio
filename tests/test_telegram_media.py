import json
import sqlite3
from unittest.mock import Mock, patch

import pytest
import requests

from services.event_store import save_event
from services.miniapp_management import list_phrases
from services.telegram_media_store import (
    remember_message, is_hidden, audio_file_id, deletion_intent, deletion_result,
)
from services.telegram_audio import download_voice, AudioUnavailable, MAX_AUDIO_BYTES
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header
from tests.test_mem_editor_persistence import service
from tests.test_telegram_outbox import RECIPIENTS


def phrase(db):
    eid = save_event("", "", "", "", "Лира 68", db)
    fix_id = f"db:{eid}"
    message = {"chat": {"id": 1001, "type": "private"}, "message_id": 777, "voice": {"file_id": "voice-test-id"}}
    remember_message(db, fix_id, message)
    return fix_id, message


def test_successful_send_links_audio_without_storing_bytes(setup):
    _, db = setup
    s = service(db)
    s.pcm_to_ogg = Mock(return_value=b"OggSfake")
    response = Mock()
    response.json.return_value = {"ok": True, "result": {
        "chat": {"id": 123}, "message_id": 777, "voice": {"file_id": "voice-test-id"}}}
    with patch("requests.post", return_value=response):
        s.send_voice(b"\0\0", "test", "db:1", RECIPIENTS)
    assert audio_file_id(db, "db:1") == "voice-test-id"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT chat_id, message_id FROM telegram_phrase_messages").fetchone() == (None, None)


def test_metadata_failure_never_retries_successful_send(setup):
    _, db = setup
    s = service(db)
    s.pcm_to_ogg = Mock(return_value=b"OggSfake")
    response = Mock()
    response.json.return_value = {"ok": True, "result": {"message_id": 777}}
    with patch("requests.post", return_value=response) as post:
        with patch("services.telegram_outbox.remember_voice", side_effect=sqlite3.OperationalError("test")):
            s.send_voice(b"\0\0", "test", "db:1", RECIPIENTS)
    assert post.call_count == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_delete_hides_phrase_and_blocks_draft_and_audio(setup, legacy):
    client, db = setup
    fix_id, message = phrase(db)
    s = service(db)
    if legacy:
        message["reply_markup"] = {"inline_keyboard": [[{"callback_data": "fix_" + fix_id}]]}
    with patch("requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        s.handle_update({"callback_query": {"id": "delete", "from": {"id": 1001}, "data": "del" if legacy else "del:" + fix_id, "message": message}})
    assert is_hidden(db, fix_id)
    assert list_phrases(db)["items"] == []
    for route in ["/api/corrections/draft", "/api/phrases/audio"]:
        assert client.get(route, params={"fix_id": fix_id}, headers=auth_header(1001)).status_code == 404
    assert client.put("/api/corrections/draft", headers=auth_header(1001),
                      json={"fix_id": fix_id, "expected_text": "Лира 68", "corrected_text": "Ліра 68"}).status_code == 404
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM radio_events").fetchone()[0] == 1


def test_failed_deletion_keeps_phrase_visible(setup):
    _, db = setup
    fix_id, message = phrase(db)
    with patch("requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "description": "Bad Request: message can't be deleted"}
        service(db).handle_update({"callback_query": {"id": "delete", "from": {"id": 1001}, "data": "del:" + fix_id, "message": message}})
    assert not is_hidden(db, fix_id)
    assert len(list_phrases(db)["items"]) == 1


def test_uncertain_delete_is_hidden_until_retried(setup):
    _, db = setup
    fix_id, message = phrase(db)
    update = {"callback_query": {"id": "delete", "from": {"id": 1001}, "data": "del:" + fix_id, "message": message}}
    with patch("requests.post", side_effect=requests.Timeout):
        with pytest.raises(requests.Timeout):
            service(db).handle_update(update)
    assert is_hidden(db, fix_id)
    with patch("requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "description": "Bad Request: message to delete not found"}
        service(db).handle_update(update)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM telegram_phrase_messages").fetchone()[0] == "deleted"


def test_sqlite_failure_prevents_remote_deletion(setup):
    _, db = setup
    fix_id, message = phrase(db)
    with patch("services.telegram_updates.deletion_intent", side_effect=sqlite3.OperationalError("test")):
        with patch("requests.post") as post:
            with pytest.raises(sqlite3.Error):
                service(db).handle_update({"callback_query": {"id": "delete", "from": {"id": 1001}, "data": "del:" + fix_id, "message": message}})
    post.assert_not_called()


def test_failed_replay_does_not_unhide_confirmed_deletion(setup):
    _, db = setup
    fix_id, _ = phrase(db)
    deletion_intent(db, fix_id)
    deletion_result(db, fix_id, True)
    deletion_intent(db, fix_id)
    deletion_result(db, fix_id, False)
    assert is_hidden(db, fix_id)
    with pytest.raises(LookupError):
        audio_file_id(db, fix_id)


def test_audio_requires_auth_and_does_not_expose_telegram_ids(setup):
    client, db = setup
    fix_id, _ = phrase(db)
    assert client.get("/api/phrases/audio", params={"fix_id": fix_id}).status_code == 401
    with patch("api.management.download_voice", return_value=b"OggSfake") as download:
        result = client.get("/api/phrases/audio", params={"fix_id": fix_id}, headers=auth_header(2002))
    assert result.status_code == 200
    assert result.content == b"OggSfake"
    assert result.headers["cache-control"] == "no-store"
    assert "voice-test-id" not in json.dumps(list_phrases(db))
    download.assert_called_once()
    assert list_phrases(db)["items"][0]["has_audio"] is True


def test_audio_upstream_error_returns_generic_502(setup):
    client, db = setup
    fix_id, _ = phrase(db)
    with patch("api.management.download_voice", side_effect=AudioUnavailable):
        result = client.get("/api/phrases/audio", params={"fix_id": fix_id}, headers=auth_header(1001))
    assert result.status_code == 502
    assert "voice-test-id" not in result.text


@pytest.mark.parametrize("path,size", [("../secret", 1), ("https://evil.test", 1), ("voice/test.oga", MAX_AUDIO_BYTES + 1)])
def test_download_rejects_unsafe_metadata(path, size):
    response = Mock()
    response.json.return_value = {"ok": True, "result": {"file_path": path, "file_size": size}}
    with patch("services.telegram_audio.requests.get", return_value=response) as get:
        with pytest.raises(AudioUnavailable):
            download_voice("test-token", "file")
    assert get.call_count == 1


@pytest.mark.parametrize("data,ok", [(b"OggSfake", True), (b"bad", False), (b"OggS" + b"x" * MAX_AUDIO_BYTES, False)])
def test_audio_download_validates_bytes_and_limits_size(data, ok):
    meta, remote = Mock(), Mock()
    meta.json.return_value = {"ok": True, "result": {"file_path": "voice/test.oga"}}
    remote.status_code = 200
    remote.iter_content.return_value = [data]
    manager = Mock()
    manager.__enter__ = Mock(return_value=remote)
    manager.__exit__ = Mock(return_value=False)
    with patch("services.telegram_audio.requests.get", side_effect=[meta, manager]):
        if ok:
            assert download_voice("test-token", "file") == data
        else:
            with pytest.raises(AudioUnavailable):
                download_voice("test-token", "file")
