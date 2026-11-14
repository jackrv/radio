import hashlib
import hmac
import json
import sqlite3
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from services.user_store import (
    bootstrap_admin,
    create_user,
    init_users_db,
)


TOKEN = "123456:unit_test_token"


def auth_header(user_id, *, auth_date=None):
    fields = {
        "auth_date": str(
            int(time.time())
            if auth_date is None
            else auth_date
        ),
        "user": json.dumps(
            {"id": user_id},
            separators=(",", ":"),
        ),
    }

    check_string = "\n".join(
        f"{key}={fields[key]}"
        for key in sorted(fields)
    )

    secret = hmac.new(
        b"WebAppData",
        TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    fields["hash"] = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Authorization": "tma " + urlencode(fields)
    }


@pytest.fixture
def setup_api(tmp_path):
    db_path = tmp_path / "api_test.db"

    init_users_db(db_path)
    bootstrap_admin(db_path, 1001)
    create_user(db_path, 1001, 2002, role="viewer")
    create_user(db_path, 1001, 3003, role="editor")

    app = create_app(db_path, TOKEN)

    with TestClient(app) as client:
        yield client, db_path


def test_admin_me(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/me",
        headers=auth_header(1001),
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": 1001,
        "role": "admin",
    }


def test_viewer_me(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/me",
        headers=auth_header(2002),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "viewer"


def test_editor_me(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/me",
        headers=auth_header(3003),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "editor"


def test_status_for_viewer(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/status",
        headers=auth_header(2002),
    )

    assert response.status_code == 200
    assert response.json() == {
        "api": "ready",
        "receiver": "offline",
    }


def test_me_without_auth(setup_api):
    client, _ = setup_api
    assert client.get("/api/me").status_code == 401


def test_status_without_auth(setup_api):
    client, _ = setup_api
    assert client.get("/api/status").status_code == 401


def test_invalid_signature(setup_api):
    client, _ = setup_api
    headers = auth_header(1001)
    value = headers["Authorization"]
    headers["Authorization"] = (
        value[:-1] + ("0" if value[-1] != "0" else "1")
    )

    assert client.get(
        "/api/me", headers=headers
    ).status_code == 401


def test_expired_auth(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/me",
        headers=auth_header(
            1001,
            auth_date=int(time.time()) - 7200,
        ),
    )

    assert response.status_code == 401


def test_unknown_user_is_denied(setup_api):
    client, _ = setup_api
    response = client.get(
        "/api/me",
        headers=auth_header(9999),
    )

    assert response.status_code == 403


def test_disabled_user_is_denied(setup_api):
    client, db_path = setup_api

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE telegram_users SET enabled=0 "
            "WHERE user_id=2002"
        )

    response = client.get(
        "/api/me",
        headers=auth_header(2002),
    )

    assert response.status_code == 403


def test_other_auth_scheme_denied(setup_api):
    client, _ = setup_api

    response = client.get(
        "/api/me",
        headers={"Authorization": "Bearer example"},
    )

    assert response.status_code == 401


def test_public_docs_disabled(setup_api):
    client, _ = setup_api

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_reads_fresh_receiver_heartbeat(setup_api):
    from services.receiver_status import (
        init_receiver_db,
        record_heartbeat,
    )

    client, db_path = setup_api

    init_receiver_db(db_path)
    record_heartbeat(db_path, "test-session")

    response = client.get(
        "/api/status",
        headers=auth_header(2002),
    )

    assert response.status_code == 200
    assert response.json() == {
        "api": "ready",
        "receiver": "listening",
    }


def test_cors_allows_github_pages(setup_api):
    client, _ = setup_api

    response = client.options(
        "/api/status",
        headers={
            "Origin": "https://jackrv.github.io",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers[
        "access-control-allow-origin"
    ] == "https://jackrv.github.io"


def test_cors_rejects_other_websites(setup_api):
    client, _ = setup_api

    response = client.options(
        "/api/status",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_rejects_write_methods(setup_api):
    client, _ = setup_api

    response = client.options(
        "/api/status",
        headers={
            "Origin": "https://jackrv.github.io",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 400


def test_cors_does_not_bypass_authentication(setup_api):
    client, _ = setup_api

    response = client.get(
        "/api/status",
        headers={"Origin": "https://jackrv.github.io"},
    )

    assert response.status_code == 401


def test_cors_allows_ngrok_skip_header(setup_api):
    client, _ = setup_api

    response = client.options(
        "/api/status",
        headers={
            "Origin": "https://jackrv.github.io",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers":
                "authorization,ngrok-skip-browser-warning",
        },
    )

    assert response.status_code == 200

    allowed = response.headers.get(
        "access-control-allow-headers", ""
    ).lower()

    assert "authorization" in allowed
    assert "ngrok-skip-browser-warning" in allowed


def test_admin_changes_notification_mode(setup_api):
    from services.bot_state import BotState

    client, db_path = setup_api

    state = BotState()
    state.enable_persistence(db_path)

    response = client.put(
        "/api/notifications/mode",
        headers=auth_header(1001),
        json={"mode": "off"},
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "off"}
    assert state.get_notify_mode() == "off"

    response = client.get(
        "/api/notifications/mode",
        headers=auth_header(1001),
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "off"}


def test_viewer_cannot_change_notification_mode(setup_api):
    client, _ = setup_api

    response = client.put(
        "/api/notifications/mode",
        headers=auth_header(2002),
        json={"mode": "off"},
    )

    assert response.status_code == 403


def test_editor_cannot_change_notification_mode(setup_api):
    client, _ = setup_api

    response = client.put(
        "/api/notifications/mode",
        headers=auth_header(3003),
        json={"mode": "off"},
    )

    assert response.status_code == 403


def test_invalid_notification_mode_rejected(setup_api):
    client, _ = setup_api

    response = client.put(
        "/api/notifications/mode",
        headers=auth_header(1001),
        json={"mode": "invalid"},
    )

    assert response.status_code == 422


def test_cors_allows_authenticated_settings_update(setup_api):
    client, _ = setup_api

    response = client.options(
        "/api/notifications/mode",
        headers={
            "Origin": "https://jackrv.github.io",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers":
                "authorization,content-type,ngrok-skip-browser-warning",
        },
    )

    assert response.status_code == 200
    assert response.headers[
        "access-control-allow-origin"
    ] == "https://jackrv.github.io"


def test_admin_can_request_receiver_pause(setup_api):
    client, _ = setup_api

    response = client.put(
        "/api/receiver/control",
        headers=auth_header(1001),
        json={"minutes": 15},
    )

    assert response.status_code == 200
    assert response.json()["pause_requested"] is True
    assert response.json()["remaining_seconds"] > 0


def test_admin_can_resume_receiver(setup_api):
    client, _ = setup_api

    client.put(
        "/api/receiver/control",
        headers=auth_header(1001),
        json={"minutes": 60},
    )

    response = client.put(
        "/api/receiver/control",
        headers=auth_header(1001),
        json={"minutes": 0},
    )

    assert response.status_code == 200
    assert response.json()["pause_requested"] is False


def test_viewer_can_read_receiver_control(setup_api):
    client, _ = setup_api

    response = client.get(
        "/api/receiver/control",
        headers=auth_header(2002),
    )

    assert response.status_code == 200
    assert response.json()["pause_requested"] is False


@pytest.mark.parametrize("user_id", [2002, 3003])
def test_non_admin_cannot_pause_receiver(setup_api, user_id):
    client, _ = setup_api

    response = client.put(
        "/api/receiver/control",
        headers=auth_header(user_id),
        json={"minutes": 15},
    )

    assert response.status_code == 403


def test_receiver_control_requires_telegram_auth(setup_api):
    client, _ = setup_api

    assert client.put(
        "/api/receiver/control",
        json={"minutes": 15},
    ).status_code == 401


@pytest.mark.parametrize("minutes", [-1, 241, True, 1.5])
def test_invalid_receiver_control_request(setup_api, minutes):
    client, _ = setup_api

    response = client.put(
        "/api/receiver/control",
        headers=auth_header(1001),
        json={"minutes": minutes},
    )

    assert response.status_code == 422


def test_receiver_api_rejects_stale_revision(setup_api):
    client, _ = setup_api
    headers = auth_header(1001)

    initial = client.get(
        "/api/receiver/control", headers=headers
    ).json()

    version = initial["revision"]

    first = client.put(
        "/api/receiver/control",
        headers=headers,
        json={
            "minutes": 15,
            "expected_revision": version,
        },
    )
    assert first.status_code == 200
    assert first.json()["revision"] == version + 1

    stale = client.put(
        "/api/receiver/control",
        headers=headers,
        json={
            "minutes": 30,
            "expected_revision": version,
        },
    )
    assert stale.status_code == 409

    current = client.get(
        "/api/receiver/control", headers=headers
    ).json()

    assert current["revision"] == version + 1
    assert current["pause_requested"] is True
    assert current["remaining_seconds"] <= 900
