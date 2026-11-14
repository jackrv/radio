"""Перший адміністратор із TELEGRAM_CHAT_ID: лише тимчасові бази, без мережі."""

import io
import json
import sqlite3
import sys
import urllib.error
from unittest.mock import Mock

import pytest

import start_radio345 as launcher
from database import init_db
from services.admin_setup import ensure_admin, fetch_private_chat, parse_private_chat
from services.sqlite_connection import get_db
from services.user_store import init_users_db

CHAT = {"id": 12345, "type": "private", "first_name": "Олена", "last_name": "Коваль", "username": "olena_k"}


def payload(**changes):
    return {"ok": True, "result": {**CHAT, **changes}}


def fake_urlopen(body, calls=None):
    def opener(url, timeout):
        if calls is not None:
            calls.append(url)
        return io.BytesIO(json.dumps(body).encode())
    return opener


def test_accepts_private_chat_with_profile():
    assert parse_private_chat(payload(), "12345") == CHAT


@pytest.mark.parametrize("changes", [{"type": "group"}, {"id": 67890}, {"id": "12345"}])
def test_rejects_group_or_mismatched_chat(changes):
    with pytest.raises(RuntimeError):
        parse_private_chat(payload(**changes), "12345")


def test_rejects_failed_response():
    with pytest.raises(RuntimeError):
        parse_private_chat({"ok": False}, "12345")


@pytest.mark.parametrize(("token", "chat_id"), [("", "12345"), ("t", "-10012345"), ("t", "")])
def test_requires_token_and_private_chat_id(token, chat_id):
    with pytest.raises(ValueError):
        fetch_private_chat(token, chat_id, urlopen=fake_urlopen(payload()))


def test_fetch_uses_get_chat_and_hides_token_in_errors():
    calls = []
    assert fetch_private_chat("secret-token", "12345", urlopen=fake_urlopen(payload(), calls))["username"] == "olena_k"
    assert "/getChat?chat_id=12345" in calls[0]

    def failing(url, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)
    with pytest.raises(RuntimeError) as error:
        fetch_private_chat("secret-token", "12345", urlopen=failing)
    assert "secret-token" not in str(error.value) and "401" in str(error.value)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    return path


def run(db_path, *, answer="y", interactive=True, fetch=None):
    messages = []
    ask = Mock(return_value=answer)
    ok = ensure_admin(
        db_path, "token", "12345", "python -m api.bootstrap_admin",
        interactive=interactive, ask=ask, out=messages.append,
        fetch=fetch or (lambda token, chat_id: dict(CHAT)),
    )
    return ok, ask, messages


def admins(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT user_id, role, enabled FROM telegram_users").fetchall()


def test_confirmed_admin_is_registered_with_name(db_path):
    ok, ask, messages = run(db_path)
    assert ok is True
    ask.assert_called_once()
    text = "\n".join(messages)
    assert "Олена Коваль" in text and "@olena_k" in text and "12345" in text
    assert admins(db_path) == [(12345, "admin", 1)]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT telegram_name, username FROM telegram_user_profiles").fetchone() == ("Олена Коваль", "olena_k")


@pytest.mark.parametrize("answer", ["", "n", "так", "ТАК"])
def test_refusal_registers_nothing(db_path, answer):
    ok, _, _ = run(db_path, answer=answer)
    assert ok is False
    init_users_db(db_path)
    assert admins(db_path) == []


def test_existing_admin_needs_no_questions(db_path):
    run(db_path)
    fetch = Mock()
    ok, ask, messages = run(db_path, fetch=fetch)
    assert ok is True and messages == []
    ask.assert_not_called()
    fetch.assert_not_called()


def test_non_interactive_console_only_explains(db_path):
    ok, ask, messages = run(db_path, interactive=False)
    assert ok is False
    ask.assert_not_called()
    assert any("api.bootstrap_admin" in line for line in messages)


def test_other_users_block_automatic_bootstrap(db_path):
    init_users_db(db_path)
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role, enabled) VALUES (777, 'admin', 0)")
    fetch = Mock()
    ok, ask, _ = run(db_path, fetch=fetch)
    assert ok is False
    fetch.assert_not_called()
    ask.assert_not_called()


def test_telegram_error_is_reported(db_path):
    def broken(token, chat_id):
        raise RuntimeError("Не вдалося перевірити чат через Telegram")
    ok, ask, messages = run(db_path, fetch=broken)
    assert ok is False
    ask.assert_not_called()
    assert any("Не вдалося" in line for line in messages)


def test_launcher_stops_before_api_without_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "api").mkdir()
    for name in ["radio.py", "api/local_server.py", ".env"]:
        (tmp_path / name).touch()
    monkeypatch.setattr(sys, "argv", ["start_radio345.py"])
    monkeypatch.setattr(launcher, "launcher_setting", lambda *args: "")
    monkeypatch.setattr(launcher, "ensure_dictionaries", lambda *args, **kwargs: True)
    monkeypatch.setattr(launcher, "interactive_console", lambda: False)
    started = Mock()
    monkeypatch.setattr(launcher, "ManagedProcess", started)
    assert launcher.main() == 2
    started.assert_not_called()
