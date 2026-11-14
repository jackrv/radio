import json
import sqlite3
import time

from fastapi.testclient import TestClient

from api.app import create_app
from services.miniapp_management import list_users, write_user
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header
from tests.test_web_auth import signed_data, TOKEN


def profile_auth(uid, **fields):
    data = signed_data(auth_date=int(time.time()), user=json.dumps({"id": uid, **fields}))
    return {"Authorization": "tma " + data}


def test_name_from_verified_telegram_data_is_saved(setup):
    _, db = setup
    with TestClient(create_app(db, TOKEN)) as client:
        response = client.get("/api/me", headers=profile_auth(3003, first_name="Олена", last_name="Тест", username="operator_test"))
    assert response.status_code == 200
    item = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    assert item["name"] == "Олена Тест"
    assert item["username"] == "operator_test"


def test_custom_name_takes_priority_and_can_be_cleared(setup):
    client, db = setup
    initial = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    assert client.put("/api/users/3003", headers=auth_header(1001), json={
        "role": "editor", "enabled": True, "display_name": "  Дневная смена  ",
        "expected_version": initial["version"],
    }).status_code == 200
    with TestClient(create_app(db, TOKEN)) as other:
        other.get("/api/me", headers=profile_auth(3003, first_name="Олена"))
    item = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    assert item["name"] == "Дневная смена"
    write_user(db, 1001, 3003, "editor", True, item["version"], "")
    assert next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")["name"] == "Олена"


def test_name_edit_uses_conflict_protection_and_existing_permissions(setup):
    client, db = setup
    item = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    body = {"role": "editor", "enabled": True, "display_name": "Редактор", "expected_version": item["version"]}
    assert client.put("/api/users/3003", headers=auth_header(3003), json=body).status_code == 403
    assert client.put("/api/users/3003", headers=auth_header(1001), json=body).status_code == 200
    assert client.put("/api/users/3003", headers=auth_header(1001), json=body).status_code == 409


def test_forged_or_unknown_profiles_are_never_saved(setup):
    _, db = setup
    with TestClient(create_app(db, TOKEN)) as client:
        auth = profile_auth(3003, first_name="Олена")
        # Change a signed value without recalculating its signature.
        auth["Authorization"] += "&extra=tampered"
        assert client.get("/api/me", headers=auth).status_code == 401
        assert client.get("/api/me", headers=profile_auth(9999, first_name="Чужой")).status_code == 403
    with sqlite3.connect(db) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_user_profiles'").fetchone()


def test_profile_refresh_does_not_change_admin_edit_version(setup):
    _, db = setup
    before = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    with TestClient(create_app(db, TOKEN)) as client:
        client.get("/api/me", headers=profile_auth(3003, first_name="Новое имя"))
    after = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    assert before["version"] == after["version"]


def test_profile_schema_is_additive_and_name_validation_is_atomic(setup):
    client, db = setup
    item = next(u for u in list_users(db, 1001)["items"] if u["user_id"] == "3003")
    for invalid in ["x" * 81, "Имя\nСтрока"]:
        assert client.put("/api/users/3003", headers=auth_header(1001), json={
            "role": "viewer", "enabled": False, "display_name": invalid, "expected_version": item["version"],
        }).status_code == 422
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT role,enabled FROM telegram_users WHERE user_id=3003").fetchone() == ("editor", 1)
    assert item["name"] == "Telegram 3003"
