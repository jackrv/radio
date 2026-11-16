"""Журнал дій користувачів Mini App і бота та сторінка «Активність» у панелі."""

import sqlite3

from fastapi.testclient import TestClient

from services import user_activity
from tests.test_mem_editor_persistence import new_phrase, setup  # noqa: F401  (фікстура)
from tests.test_web_api import auth_header


def test_record_and_open_throttling(tmp_path):
    db = tmp_path / "events.db"
    from services.user_store import init_users_db, bootstrap_admin, create_user
    init_users_db(db); bootstrap_admin(db, 1001); create_user(db, 1001, 3003, role="editor")
    user_activity.touch_open(db, 3003, now=1000)
    user_activity.touch_open(db, 3003, now=1000 + 60)                    # за хвилину — не вдруге
    user_activity.touch_open(db, 3003, now=1000 + user_activity.OPEN_INTERVAL_S)
    user_activity.record(db, 3003, "correction", "db:7", {"from": "а", "to": "б", "changed": True}, now=2000)
    user_activity.record(db, 3003, "correction", "db:8", {"from": "в", "to": "в", "changed": False}, now=2100)
    result = user_activity.overview(db, 3003, now=3000)
    assert result["actions"] == {"open": 2, "correction": 2}
    assert result["corrections_changed"] == 1 and result["role"] == "editor"
    assert [i["ts"] for i in result["items"]] == [2800, 2100, 2000, 1000], "новіші — першими"
    assert user_activity.users(db)[0]["user_id"] == 3003, "спершу не-адміністратори"


def test_record_never_breaks_the_action(tmp_path):
    user_activity.record(tmp_path / "missing-dir" / "events.db", 1, "open")   # не кидає виняток


def test_miniapp_correction_and_mode_are_recorded_with_user(setup):  # noqa: F811
    db, client = setup
    _, fix_id = new_phrase(db)
    payload = {"fix_id": fix_id, "expected_text": "Вира 68", "corrected_text": "Віра 68"}
    assert client.put("/api/corrections/draft", headers=auth_header(3003), json=payload).status_code == 200
    assert client.put("/api/notifications/mode", headers=auth_header(3003), json={"mode": "watch"}).status_code == 200
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT user_id, action, target FROM user_activity ORDER BY id").fetchall()
    assert (3003, "open", "") in rows
    assert (3003, "correction", fix_id) in rows and (3003, "mode", "watch") in rows
    stats = user_activity.overview(db, 3003)
    assert stats["corrections_changed"] == 1
    # Невдала дія не записується.
    assert client.put("/api/corrections/draft", headers=auth_header(3003), json=payload).status_code == 409
    assert user_activity.overview(db, 3003)["actions"]["correction"] == 1


def test_panel_activity_page(tmp_path):
    from api.local_dashboard import create_dashboard
    from tests.test_local_dashboard import KEY, ORIGIN
    from services.event_store import init_db
    from services.user_store import init_users_db, bootstrap_admin, create_user
    db = tmp_path / "events.db"
    init_db(db); init_users_db(db); bootstrap_admin(db, 1001); create_user(db, 1001, 3003, role="editor")
    user_activity.record(db, 3003, "correction", "db:5", {"from": "а", "to": "б", "changed": True})
    app = create_dashboard(db, KEY, samples_dir=tmp_path / "samples", recordings_dir=tmp_path / "recordings")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1)) as client:
        client.post("/session", json={"token": KEY}, headers={"Origin": ORIGIN})
        body = client.get("/api/activity").json()
        assert [u["user_id"] for u in body["users"]] == [3003, 1001]
        assert body["selected"]["user_id"] == 3003 and body["selected"]["actions"] == {"correction": 1}
        assert client.get("/api/activity", params={"user_id": 1001}).json()["selected"]["actions"] == {}
        assert client.get("/api/activity", params={"user_id": 42}).status_code == 404
