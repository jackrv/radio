import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from database import init_db
from services.event_store import init_db as init_events, save_event
from services.miniapp_management import (
    apply_startup_settings, list_phrases, read_dictionary, write_dictionary,
    ManagementConflict, list_users, write_user, read_settings, write_settings,
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
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 1, 'Віра: 68')")
    with TestClient(create_app(path, TOKEN)) as client:
        yield client, path


PANEL_ORIGIN = "http://127.0.0.1:8766"
PANEL_KEY = "k" * 43
WRITE = {"Origin": PANEL_ORIGIN}


@pytest.fixture
def panel(setup, tmp_path):
    """Локальна панель над тією самою базою: керування переїхало сюди з Mini App."""
    from api.local_dashboard import create_dashboard
    _, path = setup
    app = create_dashboard(path, PANEL_KEY, samples_dir=tmp_path / "samples",
                           diagnostics_path=tmp_path / "diagnostics.json", recordings_dir=tmp_path / "recordings")
    with TestClient(app, base_url=PANEL_ORIGIN, client=("127.0.0.1", 12345)) as client:
        assert client.post("/session", json={"token": PANEL_KEY}, headers=WRITE).status_code == 204
        yield client, path


def test_phrases_route_requires_auth(setup):
    client, _ = setup
    assert client.get("/api/phrases").status_code == 401


@pytest.mark.parametrize("method,route", [
    ("get", "overview"), ("get", "dictionaries/callsigns"), ("put", "dictionaries/callsigns"),
    ("get", "word-rules"), ("put", "word-rules/1"), ("put", "word-rules/1/approval"),
    ("get", "users"), ("put", "users/3003"), ("get", "recognition/settings"), ("put", "recognition/settings"),
    ("put", "notification-sounds/from-phrase"),
])
def test_management_is_not_published_through_the_miniapp_api(setup, method, route):
    """Керування — лише в локальній панелі; через ngrok його немає навіть для адміністратора."""
    client, _ = setup
    response = getattr(client, method)("/api/" + route, headers=auth_header(1001),
                                       **({"json": {}} if method == "put" else {}))
    assert response.status_code in (404, 405)


def test_dictionary_edit_preserves_order_disabled_rows_and_conflicts(panel):
    client, db = panel
    initial = client.get("/api/dictionaries").json()["callsigns"]
    entries = [{"raw_line": "Віра: 68 69", "enabled": True},
               {"raw_line": "Вішня: 300-310", "enabled": False},
               {"raw_line": "Вовк: *", "enabled": True}]
    payload = {"entries": entries, "expected_version": initial["version"]}
    response = client.put("/api/dictionaries/callsigns", headers=WRITE, json=payload)
    assert response.status_code == 200
    assert response.json()["entries"] == entries
    import sqlite3
    from services.dictionary_loader import read_dictionary_lines
    with sqlite3.connect(db) as conn:
        assert read_dictionary_lines(conn)["callsigns"] == ["Віра: 68 69", "Вовк: *"]
    assert client.put("/api/dictionaries/callsigns", headers=WRITE, json=payload).status_code == 409


@pytest.mark.parametrize("entries", [[], [{"raw_line": "# comment", "enabled": True}],
    [{"raw_line": "Віра: 900-1", "enabled": True}],
    [{"raw_line": "Віра: 1-999999999", "enabled": True}],
    [{"raw_line": "Віра: 6x", "enabled": True}],
    [{"raw_line": "Віра:", "enabled": True}],
    [{"raw_line": "Віра 68 69", "enabled": True}],
    [{"raw_line": "Віра 68-69", "enabled": True}],
    [{"raw_line": "Віра *", "enabled": True}],
    [{"raw_line": "Віра\nВовк", "enabled": True}],
    [{"raw_line": "Віра", "enabled": False}],
])
def test_invalid_dictionary_leaves_original_intact(panel, entries):
    client, db = panel
    initial = read_dictionary(db, "callsigns")
    result = client.put("/api/dictionaries/callsigns", headers=WRITE,
                        json={"entries": entries, "expected_version": initial["version"]})
    assert result.status_code == 422
    assert read_dictionary(db, "callsigns") == initial


def test_viewer_cannot_write_dictionary(setup):
    _, db = setup
    initial = read_dictionary(db, "callsigns")
    with pytest.raises(PermissionError):
        write_dictionary(db, 2002, "callsigns", initial["entries"], initial["version"])


def test_dictionary_transaction_rolls_back_deleted_rows(setup):
    _, db = setup
    initial = read_dictionary(db, "callsigns")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER fail_dictionary BEFORE INSERT ON dictionary_entries BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.Error):
        write_dictionary(db, 1001, "callsigns", [{"raw_line": "Вовк", "enabled": True}], initial["version"])
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
        assert sorted(pool.map(edit, ["Віра: 69", "Вішня: 700"])) == ["conflict", "saved"]


def test_phrases_unifies_db_and_mem_and_excludes_expired(setup):
    client, db = setup
    eid = save_event("", "", "", "", "Віра 68", db, raw_text="Вира 68")
    register_phrase(db, "a" * 32, "Вовк 100%")
    register_phrase(db, "b" * 32, "expired")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_recent_phrases SET created_at=0 WHERE token=?", ("b" * 32,))
    response = client.get("/api/phrases?limit=1", headers=auth_header(3003))
    assert response.status_code == 200
    assert response.json()["has_more"] is True
    items = list_phrases(db)["items"]
    assert {item["fix_id"] for item in items} == {f"db:{eid}", "mem:" + "a" * 32}
    assert list_phrases(db, "%")["items"][0]["current_text"] == "Вовк 100%"
    assert len(list_phrases(db, "вир")["items"]) == 1
    assert len(list_phrases(db, offset=1, limit=1)["items"]) == 1
    assert list_phrases(db, offset=2)["items"] == []


def _rule(path):
    from services.miniapp_management import learning_queue
    return learning_queue(path)["items"][0]


def test_rules_disable_and_reenable_respect_confirmations(panel):
    client, db = panel
    save_word_feedback(db, "first", "Вира 68", "Віра 68")
    initial = _rule(db)
    assert initial["status"] == "pending"
    url = f'/api/learning/{initial["id"]}'
    assert client.put(url, headers=WRITE, json={"disabled": True, "expected_version": initial["version"]}).status_code == 200
    assert client.put(url, headers=WRITE, json={"disabled": False, "expected_version": initial["version"]}).status_code == 409
    disabled = _rule(db)
    assert client.put(url, headers=WRITE, json={"disabled": False, "expected_version": disabled["version"]}).status_code == 200
    assert load_active_word_rules(db) == []
    save_word_feedback(db, "second", "Вира 69", "Віра 69")
    assert load_active_word_rules(db) == [("вира", "віра")]


def test_rule_toggle_forbidden_to_viewer(setup):
    _, db = setup
    from services.miniapp_management import toggle_rule
    save_word_feedback(db, "first", "Вира 68", "Віра 68")
    item = _rule(db)
    with pytest.raises(PermissionError):
        toggle_rule(db, 2002, item["id"], True, item["version"])


def test_user_management_and_stale_edit(setup, panel):
    api, _ = setup
    client, _ = panel
    # Keep IDs as strings on the wire; JS must not round 64-bit SQLite identifiers.
    uid = "9007199254740993"
    assert client.put("/api/users/" + uid, headers=WRITE, json={"role": "editor", "enabled": True}).status_code == 200
    new = next(u for u in client.get("/api/users").json()["items"] if u["user_id"] == uid)
    payload = {"role": "viewer", "enabled": False, "expected_version": new["version"]}
    assert client.put("/api/users/" + uid, headers=WRITE, json=payload).status_code == 200
    assert client.put("/api/users/" + uid, headers=WRITE, json=payload).status_code == 409
    assert client.put("/api/users/" + uid, json=payload).status_code == 403, "без Origin панелі"
    assert api.get("/api/me", headers=auth_header(int(uid))).status_code == 403


@pytest.mark.parametrize("uid", [2002, 3003])
def test_non_admin_cannot_manage_users(setup, uid):
    _, db = setup
    with pytest.raises(PermissionError):
        write_user(db, uid, 4004, "admin", True, None)


def test_cannot_remove_last_admin(setup, panel):
    api, _ = setup
    client, _ = panel
    admin = next(u for u in client.get("/api/users").json()["items"] if u["role"] == "admin")
    assert client.put("/api/users/1001", headers=WRITE,
                      json={"role": "viewer", "enabled": False, "expected_version": admin["version"]}).status_code == 422
    assert api.get("/api/me", headers=auth_header(1001)).status_code == 200


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


def test_settings_persist_apply_at_startup_and_conflict(panel):
    client, db = panel
    initial = read_settings(db)
    values = {**initial["values"], "USE_LLM": False, "VAD_LEVEL": 3, "MIN_SPEECH_MS": 300}
    payload = {"values": values, "expected_revision": initial["revision"]}
    response = client.put("/api/settings", headers=WRITE, json=payload)
    assert response.status_code == 200
    assert response.json()["requires_restart"] is True
    assert client.put("/api/settings", headers=WRITE, json=payload).status_code == 409
    target = SimpleNamespace()
    apply_startup_settings(db, target)
    assert vars(target) == values
    assert read_settings(db)["values"] == values


@pytest.mark.parametrize("key,value", [("USE_LLM", "false"), ("VAD_LEVEL", True), ("VAD_LEVEL", 4),
                                      ("MIN_SPEECH_MS", 0), ("MAX_SEGMENT_S", 1000)])
def test_settings_validation(panel, key, value):
    client, db = panel
    data = read_settings(db)
    assert client.put("/api/settings", headers=WRITE,
                      json={"values": {**data["values"], key: value}, "expected_revision": 0}).status_code == 422


def test_non_admin_cannot_save_settings(setup):
    _, db = setup
    data = read_settings(db)
    with pytest.raises(PermissionError):
        write_settings(db, 3003, data["values"], data["revision"])


def test_management_sqlite_error_is_503(setup):
    client, _ = setup
    with patch("services.miniapp_management.list_phrases", side_effect=sqlite3.OperationalError("private details")):
        response = client.get("/api/phrases", headers=auth_header(1001))
    assert response.status_code == 503
    assert "private details" not in response.text


def test_new_routes_reject_disabled_user(setup):
    client, db = setup
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=3003")
    assert client.get("/api/phrases", headers=auth_header(3003)).status_code == 403


def test_overview_reports_actual_counts(setup):
    _, db = setup
    from services.miniapp_management import overview
    save_event("", "", "", "", "Віра 68", db)
    data = overview(db)
    assert data["events"] == 1
    assert data["dictionary_entries"] == 1
