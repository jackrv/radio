"""Persistent temporary Telegram phrases and Mini App editor."""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from services.event_store import init_db as init_event_db
from services.telegram import TelegramService
from services.telegram_phrase_store import (
    RETENTION_SECONDS, load_phrase, register_phrase,
)
from services.miniapp_correction_editor import (
    CorrectionConflict, save_edit_draft,
)
from services.user_store import init_users_db, bootstrap_admin, create_user
from tests.test_web_api import TOKEN, auth_header


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "events.db"
    init_event_db(db)
    init_users_db(db)
    bootstrap_admin(db, 1001)
    create_user(db, 1001, 2002, role="viewer")
    create_user(db, 1001, 3003, role="editor")
    with TestClient(create_app(db, TOKEN)) as client:
        yield db, client


def service(db):
    return TelegramService(
        bot_token="test",
        sample_rate=16000,
        db_path=str(db),
        log=Mock(),
    )


def new_phrase(db, text="Лира 68"):
    s = service(db)
    fix_id = s.register_phrase_for_fix(text, None)
    assert fix_id.startswith("mem:")
    assert len(fix_id) == 36
    return s, fix_id


def test_new_mem_keyboard_has_delete_and_web_editor_only(setup):
    db, _ = setup
    s, fix_id = new_phrase(db)
    rows = json.loads(s.get_keyboard(fix_id))["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0][0]["callback_data"] == "del:" + fix_id
    assert rows[1][0]["web_app"]["url"].endswith(
        "?fix_id=" + fix_id.replace(":", "%3A")
    )


def test_mem_phrase_survives_restart(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    service(db)  # новий екземпляр після перезапуску
    response = client.get("/api/corrections/draft", params={"fix_id": fix_id},
                          headers=auth_header(1001))
    assert response.json()["original_text"] == "Лира 68"


def test_editor_prefills_memory_text(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    response = client.get(
        "/api/corrections/draft", params={"fix_id": fix_id},
        headers=auth_header(3003),
    )
    assert response.status_code == 200
    assert response.json() == {
        "fix_id": fix_id,
        "original_text": "Лира 68",
        "current_text": "Лира 68",
        "has_audio": False,
    }


def test_editor_saves_word_vote_and_can_be_reopened(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    payload = {"fix_id": fix_id,
               "expected_text": "Лира 68", "corrected_text": "Ліра 68"}
    response = client.put("/api/corrections/draft",
                          headers=auth_header(3003), json=payload)
    assert response.status_code == 200
    assert response.json()["saved"] is True
    current = client.get("/api/corrections/draft",
                         params={"fix_id": fix_id},
                         headers=auth_header(3003)).json()
    assert current["current_text"] == "Ліра 68"
    assert current["original_text"] == "Лира 68"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0] == 1


def test_duplicate_mem_edit_is_conflict_and_not_double_vote(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    payload = {"fix_id": fix_id,
               "expected_text": "Лира 68", "corrected_text": "Ліра 68"}
    assert client.put("/api/corrections/draft",
                      headers=auth_header(1001), json=payload).status_code == 200
    assert client.put("/api/corrections/draft",
                      headers=auth_header(1001), json=payload).status_code == 409
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM word_correction_votes"
                            ).fetchone()[0] == 1


def test_viewer_cannot_access_mem_draft(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    assert client.get("/api/corrections/draft", params={"fix_id": fix_id},
                      headers=auth_header(2002)).status_code == 403
    assert client.put("/api/corrections/draft", headers=auth_header(2002),
                      json={"fix_id": fix_id,
                            "expected_text": "Лира 68",
                            "corrected_text": "Ліра 68"}).status_code == 403


def test_old_mem_id_is_rejected_by_editor_api(setup):
    _, client = setup
    assert client.get("/api/corrections/draft", params={"fix_id": "mem:abcd"},
                      headers=auth_header(1001)).status_code == 422


def test_expired_mem_phrase_is_not_available(setup):
    db, client = setup
    s, fix_id = new_phrase(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE telegram_recent_phrases SET created_at = ?",
                     (int(time.time()) - RETENTION_SECONDS - 1,))
    assert client.get("/api/corrections/draft", params={"fix_id": fix_id},
                      headers=auth_header(1001)).status_code == 404


def test_recent_phrase_limit_prunes_oldest(setup):
    db, _ = setup
    s = service(db)
    s.recent_phrases_max = 2
    first = s.register_phrase_for_fix("Лира 68", None)
    second = s.register_phrase_for_fix("На ган 110", None)
    third = s.register_phrase_for_fix("На ган 112", None)
    assert load_phrase(db, first[4:]) is None
    assert load_phrase(db, second[4:]) is not None
    assert load_phrase(db, third[4:]) is not None
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM telegram_recent_phrases"
                             ).fetchone()[0]
    assert count == 2


def test_word_feedback_failure_rolls_back_mem_edit(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER fail_vote BEFORE INSERT ON word_correction_votes
            BEGIN SELECT RAISE(ABORT, 'test'); END
        """)
    response = client.put("/api/corrections/draft",
                          headers=auth_header(1001),
                          json={"fix_id": fix_id, "expected_text": "Лира 68",
                                "corrected_text": "Ліра 68"})
    assert response.status_code == 503
    assert load_phrase(db, fix_id[4:])["current_text"] == "Лира 68"


def test_successive_edits_replace_votes(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    for expected, corrected in [("Лира 68", "Ліра 68"), ("Ліра 68", "Ліро 68")]:
        assert client.put(
            "/api/corrections/draft", headers=auth_header(3003),
            json={"fix_id": fix_id, "expected_text": expected,
                  "corrected_text": corrected},
        ).status_code == 200
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_id, original, corrected FROM word_correction_votes"
        ).fetchall() == [(fix_id, "лира", "ліро")]
        assert conn.execute(
            "SELECT SUM(confirmations) FROM word_corrections"
        ).fetchone()[0] == 1
    assert load_phrase(db, fix_id[4:])["current_text"] == "Ліро 68"


def test_expiry_boundary_and_edits_do_not_extend_lifetime(setup):
    db, _ = setup
    with patch("services.telegram_phrase_store.time.time", return_value=100000):
        _, fix_id = new_phrase(db)
    with patch("services.telegram_phrase_store.time.time", return_value=186399):
        save_edit_draft(db, fix_id, "Лира 68", "Ліра 68")
        assert load_phrase(db, fix_id[4:]) is not None
    with patch("services.telegram_phrase_store.time.time", return_value=186400):
        assert load_phrase(db, fix_id[4:]) is None
        with pytest.raises(LookupError):
            save_edit_draft(db, fix_id, "Ліра 68", "Ліро 68")


def test_registration_prunes_expired_rows_and_enforces_200_limit(setup):
    db, _ = setup
    with patch("services.telegram_phrase_store.time.time", return_value=100000):
        _, expired = new_phrase(db)
    with patch("services.telegram_phrase_store.time.time", return_value=200000):
        s = service(db)
        ids = [s.register_phrase_for_fix(f"Фраза {i}", None) for i in range(201)]
        assert load_phrase(db, expired[4:]) is None
        assert load_phrase(db, ids[0][4:]) is None
        assert load_phrase(db, ids[-1][4:]) is not None
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM telegram_recent_phrases"
            ).fetchone()[0] == 200
    with pytest.raises(ValueError):
        register_phrase(db, "f" * 32, "Фраза", max_phrases=201)


def test_parallel_edits_have_one_winner(setup):
    db, _ = setup
    _, fix_id = new_phrase(db)
    barrier = threading.Barrier(2)

    def edit(text):
        barrier.wait(timeout=5)
        try:
            save_edit_draft(db, fix_id, "Лира 68", text)
            return "saved"
        except CorrectionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["Ліра 68", "Ліро 68"])) == ["conflict", "saved"]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM word_correction_votes"
        ).fetchone()[0] == 1


def test_phrase_update_failure_rolls_back_votes(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TRIGGER fail_phrase_update
            BEFORE UPDATE ON telegram_recent_phrases
            BEGIN SELECT RAISE(ABORT, 'test'); END
        """)
    assert client.put(
        "/api/corrections/draft", headers=auth_header(1001),
        json={"fix_id": fix_id, "expected_text": "Лира 68",
              "corrected_text": "Ліра 68"},
    ).status_code == 503
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM word_correction_votes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM word_corrections").fetchone()[0] == 0
    assert load_phrase(db, fix_id[4:])["current_text"] == "Лира 68"


def test_sqlite_registration_failure_leaves_only_delete_button(setup):
    db, _ = setup
    s = service(db)
    with patch("services.telegram_outbox.register_phrase", side_effect=sqlite3.OperationalError("test")):
        fix_id = s.register_phrase_for_fix("Лира 68", None)
    assert fix_id is None
    rows = json.loads(s.get_keyboard(fix_id))["inline_keyboard"]
    assert rows == [[{"text": "🗑 Видалити", "callback_data": "del"}]]


def test_mem_api_requires_authentication(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    assert client.get(
        "/api/corrections/draft", params={"fix_id": fix_id}
    ).status_code == 401
    assert client.put("/api/corrections/draft", json={
        "fix_id": fix_id, "expected_text": "Лира 68", "corrected_text": "Ліра 68",
    }).status_code == 401


def test_parallel_registration_respects_limit(setup):
    db, _ = setup

    def register(index):
        register_phrase(db, f"{index:032x}", f"Фраза {index}", max_phrases=2)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(register, range(12)))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM telegram_recent_phrases"
        ).fetchone()[0] == 2


def test_sqlite_read_failure_returns_503(setup):
    db, client = setup
    _, fix_id = new_phrase(db)
    with patch("services.miniapp_correction_editor.load_phrase",
               side_effect=sqlite3.OperationalError("test")):
        assert client.get(
            "/api/corrections/draft", params={"fix_id": fix_id},
            headers=auth_header(1001),
        ).status_code == 503
