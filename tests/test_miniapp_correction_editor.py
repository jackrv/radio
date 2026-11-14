import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from database import init_db as init_dictionary_db
from services.event_store import init_db as init_event_db, save_event
from services.user_store import init_users_db, bootstrap_admin, create_user
from tests.test_web_api import TOKEN, auth_header


@pytest.fixture
def client_and_db(tmp_path):
    db = tmp_path / "events.db"
    init_dictionary_db(db)
    init_event_db(db)
    init_users_db(db)
    bootstrap_admin(db, 1001)
    create_user(db, 1001, 2002, role="viewer")
    create_user(db, 1001, 3003, role="editor")
    eid = save_event(
        "", "", "", "", "Ліра 68, прийом", db,
        raw_text="Ліра 68, прийом",
    )
    with TestClient(create_app(db, TOKEN)) as client:
        yield client, db, eid


def test_editor_get_prefilled_phrase(client_and_db):
    client, _, eid = client_and_db
    response = client.get(
        "/api/corrections/draft", params={"fix_id": f"db:{eid}"},
        headers=auth_header(3003),
    )
    assert response.status_code == 200
    assert response.json() == {
        "fix_id": f"db:{eid}",
        "original_text": "Ліра 68, прийом",
        "current_text": "Ліра 68, прийом",
        "has_audio": False,
    }


def test_editor_saves_text_and_word_vote(client_and_db):
    client, db, eid = client_and_db
    response = client.put(
        "/api/corrections/draft", headers=auth_header(3003),
        json={
            "fix_id": f"db:{eid}",
            "expected_text": "Ліра 68, прийом",
            "corrected_text": "Ліра 68, зв'язок",
        },
    )
    assert response.status_code == 200
    assert response.json()["saved"] is True
    with sqlite3.connect(db) as conn:
        text = conn.execute(
            "SELECT text FROM radio_events WHERE id=?", (eid,)
        ).fetchone()[0]
        votes = conn.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0]
    assert text == "Ліра 68, зв'язок"
    assert votes >= 1


def test_stale_editor_cannot_overwrite(client_and_db):
    client, db, eid = client_and_db
    body = {
        "fix_id": f"db:{eid}",
        "expected_text": "Ліра 68, прийом",
        "corrected_text": "Ліра 68, зв'язок",
    }
    first = client.put(
        "/api/corrections/draft", headers=auth_header(1001), json=body
    )
    assert first.status_code == 200
    second = client.put(
        "/api/corrections/draft", headers=auth_header(1001),
        json={**body, "corrected_text": "Ліра 68, інший текст"},
    )
    assert second.status_code == 409
    with sqlite3.connect(db) as conn:
        text = conn.execute(
            "SELECT text FROM radio_events WHERE id=?", (eid,)
        ).fetchone()[0]
    assert text == "Ліра 68, зв'язок"


def test_viewer_cannot_read_or_change_draft(client_and_db):
    client, _, eid = client_and_db
    header = auth_header(2002)
    assert client.get(
        "/api/corrections/draft", params={"fix_id": f"db:{eid}"},
        headers=header,
    ).status_code == 403
    assert client.put(
        "/api/corrections/draft", headers=header,
        json={
            "fix_id": f"db:{eid}",
            "expected_text": "Ліра 68, прийом",
            "corrected_text": "Ліра 68, зв'язок",
        },
    ).status_code == 403


def test_bad_id_and_empty_text_rejected(client_and_db):
    client, _, eid = client_and_db
    headers = auth_header(1001)
    assert client.get(
        "/api/corrections/draft", params={"fix_id": "mem:abc"},
        headers=headers,
    ).status_code == 422
    assert client.put(
        "/api/corrections/draft", headers=headers,
        json={
            "fix_id": f"db:{eid}",
            "expected_text": "Ліра 68, прийом",
            "corrected_text": "   ",
        },
    ).status_code == 422


def test_editor_requires_telegram_auth(client_and_db):
    client, _, eid = client_and_db
    assert client.get(
        "/api/corrections/draft", params={"fix_id": f"db:{eid}"},
    ).status_code == 401


def test_bot_keyboard_opens_web_editor():
    import json
    from unittest.mock import Mock
    from services.telegram import TelegramService

    service = TelegramService(
        bot_token="test",
        sample_rate=16000,
        db_path=":memory:",
        log=Mock(),
    )
    rows = json.loads(service.get_keyboard("db:97"))["inline_keyboard"]
    assert rows[1][0]["web_app"]["url"] == (
        "https://jackrv.github.io/radio/?fix_id=db%3A97"
    )
    assert len(rows) == 2
    # Короткий застарілий mem: без редактора — лише видалення.
    assert json.loads(service.get_keyboard("mem:abc"))["inline_keyboard"] == [
        [{"text": "🗑 Видалити", "callback_data": "del"}]
    ]


def test_failed_word_vote_rolls_back_event_edit(client_and_db):
    client, db, eid = client_and_db
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER reject_vote
            BEFORE INSERT ON word_correction_votes
            BEGIN
                SELECT RAISE(ABORT, 'word vote failed');
            END
        """)
    result = client.put(
        "/api/corrections/draft", headers=auth_header(1001),
        json={
            "fix_id": f"db:{eid}",
            "expected_text": "Ліра 68, прийом",
            "corrected_text": "Ліра 68, зв'язок",
        },
    )
    assert result.status_code == 503
    with sqlite3.connect(db) as conn:
        current = conn.execute(
            "SELECT text FROM radio_events WHERE id=?", (eid,)
        ).fetchone()[0]
    assert current == "Ліра 68, прийом"
