import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from database import init_db, get_dictionary_lines
from services.event_store import init_db as init_events, save_event
from services.miniapp_management import (
    apply_startup_settings, list_phrases, read_dictionary, write_dictionary,
    ManagementConflict, list_users, write_user,
)
from services.telegram_phrase_store import register_phrase
from services.user_store import init_users_db, bootstrap_admin, create_user
from services.word_store import save_word_feedback, load_active_word_rules
from tests.test_web_api import TOKEN, auth_header


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "management.db"
    init_db(path)
    init_events(path)
    init_users_db(path)
    bootstrap_admin(path, 1001)
    create_user(path, 1001, 2002, role="viewer")
    create_user(path, 1001, 3003, role="editor")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 1, 'Ліра: 68')")
    with TestClient(create_app(path, TOKEN)) as client:
        yield client, path


@pytest.mark.parametrize("route", ["overview", "phrases", "dictionaries/callsigns", "word-rules", "users", "recognition/settings"])
def test_all_new_read_routes_require_auth(setup, route):
    client, _ = setup
    assert client.get("/api/" + route).status_code == 401


@pytest.mark.parametrize("uid", [2002, 3003])
@pytest.mark.parametrize("route", ["users", "recognition/settings"])
def test_restricted_read_routes(setup, uid, route):
    client, _ = setup
    assert client.get("/api/" + route, headers=auth_header(uid)).status_code == 403


def test_dictionary_edit_preserves_order_disabled_rows_and_conflicts(setup):
    client, db = setup
    headers = auth_header(3003)
    initial = client.get("/api/dictionaries/callsigns", headers=headers).json()
    entries = [{"raw_line": "Ліра: 68 69", "enabled": True},
               {"raw_line": "Ліпка: 300-310", "enabled": False},
               {"raw_line": "Вовк: *", "enabled": True}]
    payload = {"entries": entries, "expected_version": initial["version"]}
    response = client.put("/api/dictionaries/callsigns", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["entries"] == entries
    assert get_dictionary_lines("callsigns", db) == ["Ліра: 68 69", "Вовк: *"]
    assert client.put("/api/dictionaries/callsigns", headers=headers, json=payload).status_code == 409


@pytest.mark.parametrize("entries", [[], [{"raw_line": "# comment", "enabled": True}],
    [{"raw_line": "Ліра: 900-1", "enabled": True}],
    [{"raw_line": "Ліра: 1-999999999", "enabled": True}],
    [{"raw_line": "Ліра: 6x", "enabled": True}],
    [{"raw_line": "Ліра:", "enabled": True}],
    [{"raw_line": "Ліра 68 69", "enabled": True}],
    [{"raw_line": "Ліра 68-69", "enabled": True}],
    [{"raw_line": "Ліра *", "enabled": True}],
    [{"raw_line": "Ліра\nВовк", "enabled": True}],
    [{"raw_line": "Ліра", "enabled": False}],
])
def test_invalid_dictionary_leaves_original_intact(setup, entries):
    client, db = setup
    initial = read_dictionary(db, "callsigns")
    result = client.put("/api/dictionaries/callsigns", headers=auth_header(1001),
                        json={"entries": entries, "expected_version": initial["version"]})
    assert result.status_code == 422
    assert read_dictionary(db, "callsigns") == initial


def test_viewer_cannot_write_dictionary(setup):
    client, db = setup
    initial = read_dictionary(db, "callsigns")
    assert client.put("/api/dictionaries/callsigns", headers=auth_header(2002),
                      json={"entries": initial["entries"], "expected_version": initial["version"]}).status_code == 403


def test_dictionary_transaction_rolls_back_deleted_rows(setup):
    client, db = setup
    initial = read_dictionary(db, "callsigns")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER fail_dictionary BEFORE INSERT ON dictionary_entries BEGIN SELECT RAISE(ABORT, 'test'); END")
    assert client.put("/api/dictionaries/callsigns", headers=auth_header(1001),
                      json={"entries": [{"raw_line": "Вовк", "enabled": True}],
                            "expected_version": initial["version"]}).status_code == 503
    assert read_dictionary(db, "callsigns") == initial


def test_parallel_dictionary_updates_have_one_winner(setup):
    _, db = setup
    version = read_dictionary(db, "callsigns")["version"]
    barrier = threading.Barrier(2)

    def edit(text):
        barrier.wait(timeout=5)
        try:
            write_dictionary(db, 3003, "callsigns", [{"raw_line": text, "enabled": True}], version)
            return "saved"
        except ManagementConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, ["Ліра: 69", "Ліпка: 700"])) == ["conflict", "saved"]


def test_phrases_unifies_db_and_mem_and_excludes_expired(setup):
    client, db = setup
    eid = save_event("", "", "", "", "Ліра 68", db, raw_text="Лира 68")
    register_phrase(db, "a" * 32, "Вовк 100%")
    register_phrase(db, "b" * 32, "expired")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_recent_phrases SET created_at=0 WHERE token=?", ("b" * 32,))
    response = client.get("/api/phrases?limit=1", headers=auth_header(2002))
    assert response.status_code == 200
    assert response.json()["has_more"] is True
    items = list_phrases(db)["items"]
    assert {item["fix_id"] for item in items} == {f"db:{eid}", "mem:" + "a" * 32}
    assert list_phrases(db, "%")["items"][0]["current_text"] == "Вовк 100%"
    assert len(list_phrases(db, "лир")["items"]) == 1
    assert len(list_phrases(db, offset=1, limit=1)["items"]) == 1
    assert list_phrases(db, offset=2)["items"] == []


def test_rules_disable_and_reenable_respect_confirmations(setup):
    client, db = setup
    save_word_feedback(db, "first", "Лира 68", "Ліра 68")
    initial = client.get("/api/word-rules", headers=auth_header(2002)).json()["items"][0]
    assert initial["status"] == "pending"
    url = f'/api/word-rules/{initial["id"]}'
    headers = auth_header(3003)
    assert client.put(url, headers=headers, json={"disabled": True, "expected_version": initial["version"]}).status_code == 200
    assert client.put(url, headers=headers, json={"disabled": False, "expected_version": initial["version"]}).status_code == 409
    disabled = client.get("/api/word-rules", headers=headers).json()["items"][0]
    assert client.put(url, headers=headers, json={"disabled": False, "expected_version": disabled["version"]}).status_code == 200
    assert load_active_word_rules(db) == []
    save_word_feedback(db, "second", "Лира 69", "Ліра 69")
    assert load_active_word_rules(db) == [("лира", "ліра")]


def test_rule_toggle_forbidden_to_viewer(setup):
    client, db = setup
    save_word_feedback(db, "first", "Лира 68", "Ліра 68")
    item = client.get("/api/word-rules", headers=auth_header(2002)).json()["items"][0]
    assert client.put(f'/api/word-rules/{item["id"]}', headers=auth_header(2002),
                      json={"disabled": True, "expected_version": item["version"]}).status_code == 403


def test_user_management_and_stale_edit(setup):
    client, _ = setup
    headers = auth_header(1001)
    # Keep IDs as strings on the wire; JS must not round 64-bit SQLite identifiers.
    uid = "9007199254740993"
    assert client.put("/api/users/" + uid, headers=headers,
                      json={"role": "editor", "enabled": True}).status_code == 200
    users = client.get("/api/users", headers=headers).json()["items"]
    new = next(u for u in users if u["user_id"] == uid)
    payload = {"role": "viewer", "enabled": False, "expected_version": new["version"]}
    assert client.put("/api/users/" + uid, headers=headers, json=payload).status_code == 200
    assert client.put("/api/users/" + uid, headers=headers, json=payload).status_code == 409
    assert client.get("/api/me", headers=auth_header(int(uid))).status_code == 403


@pytest.mark.parametrize("uid", [2002, 3003])
def test_non_admin_cannot_manage_users(setup, uid):
    client, _ = setup
    assert client.put("/api/users/4004", headers=auth_header(uid),
                      json={"role": "admin", "enabled": True}).status_code == 403


def test_cannot_remove_last_admin(setup):
    client, _ = setup
    headers = auth_header(1001)
    admin = next(u for u in client.get("/api/users", headers=headers).json()["items"] if u["role"] == "admin")
    assert client.put("/api/users/1001", headers=headers,
                      json={"role": "viewer", "enabled": False, "expected_version": admin["version"]}).status_code == 422
    assert client.get("/api/me", headers=headers).status_code == 200


def test_parallel_admin_demotions_leave_one_active_admin(setup):
    _, db = setup
    write_user(db, 1001, 4004, "admin", True, None)
    users = {u["user_id"]: u for u in list_users(db, 1001)["items"]}
    barrier = threading.Barrier(2)

    def demote(uid):
        barrier.wait(timeout=5)
        try:
            write_user(db, uid, uid, "viewer", True, users[str(uid)]["version"])
            return "saved"
        except ValueError:
            return "last-admin"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(demote, [1001, 4004])) == ["last-admin", "saved"]


def test_settings_persist_apply_at_startup_and_conflict(setup):
    client, db = setup
    headers = auth_header(1001)
    initial = client.get("/api/recognition/settings", headers=headers).json()
    values = {**initial["values"], "USE_LLM": False, "VAD_LEVEL": 3, "MIN_SPEECH_MS": 300}
    payload = {"values": values, "expected_revision": initial["revision"]}
    response = client.put("/api/recognition/settings", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["requires_restart"] is True
    assert client.put("/api/recognition/settings", headers=headers, json=payload).status_code == 409
    target = SimpleNamespace()
    apply_startup_settings(db, target)
    assert vars(target) == values
    # A new API instance sees the same SQLite state.
    with TestClient(create_app(db, TOKEN)) as restarted:
        assert restarted.get("/api/recognition/settings", headers=headers).json()["values"] == values


@pytest.mark.parametrize("key,value", [("USE_LLM", "false"), ("VAD_LEVEL", True), ("VAD_LEVEL", 4),
                                      ("MIN_SPEECH_MS", 0), ("MAX_SEGMENT_S", 1000)])
def test_settings_validation(setup, key, value):
    client, _ = setup
    headers = auth_header(1001)
    data = client.get("/api/recognition/settings", headers=headers).json()
    assert client.put("/api/recognition/settings", headers=headers,
                      json={"values": {**data["values"], key: value}, "expected_revision": 0}).status_code == 422


def test_non_admin_cannot_save_settings(setup):
    client, _ = setup
    values = client.get("/api/recognition/settings", headers=auth_header(1001)).json()["values"]
    assert client.put("/api/recognition/settings", headers=auth_header(3003),
                      json={"values": values, "expected_revision": 0}).status_code == 403


def test_management_sqlite_error_is_503(setup):
    client, _ = setup
    with patch("services.miniapp_management.overview", side_effect=sqlite3.OperationalError("private details")):
        response = client.get("/api/overview", headers=auth_header(1001))
    assert response.status_code == 503
    assert "private details" not in response.text


def test_new_routes_reject_disabled_user(setup):
    client, db = setup
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=3003")
    assert client.get("/api/phrases", headers=auth_header(3003)).status_code == 403


def test_overview_reports_actual_counts(setup):
    client, db = setup
    save_event("", "", "", "", "Ліра 68", db)
    response = client.get("/api/overview", headers=auth_header(2002))
    assert response.status_code == 200
    assert response.json()["events"] == 1
    assert response.json()["dictionary_entries"] == 1
    assert response.headers["cache-control"] == "no-store"
