import ast
import logging
import sqlite3

from pathlib import Path
from unittest.mock import Mock

import pytest

from services.bot_state import BotState
from services.telegram import TelegramService
import services.bot_state as bot_state_module


def make_state(tmp_path):
    path = str(tmp_path / "events.db")
    state = BotState()
    state.enable_persistence(path)
    return state, path


def test_default_mode_on_first_start(tmp_path):
    state, path = make_state(tmp_path)

    assert state.get_notify_mode() == "all"

    with sqlite3.connect(path) as db:
        tables = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'bot_settings'
            """
        ).fetchall()

    assert tables == [("bot_settings",)]


def test_mode_survives_restart(tmp_path):
    state, path = make_state(tmp_path)

    with state.lock:
        state.set_notify_mode("watch")

    restarted = BotState()
    restarted.enable_persistence(path)

    assert restarted.get_notify_mode() == "watch"

    with restarted.lock:
        restarted.set_notify_mode("commands")

    third = BotState()
    third.enable_persistence(path)

    assert third.get_notify_mode() == "commands"


def test_invalid_saved_mode_falls_back_to_all(
    tmp_path, caplog
):
    _, path = make_state(tmp_path)

    with sqlite3.connect(path) as db:
        db.execute(
            """
            INSERT INTO bot_settings
                (setting_key, setting_value)
            VALUES (?, ?)
            """,
            ("notify_mode", "incorrect_mode"),
        )

    restarted = BotState()

    with caplog.at_level(logging.WARNING):
        restarted.enable_persistence(path)

    assert restarted.get_notify_mode() == "all"
    assert "Недопустимый сохранённый режим" in caplog.text


def test_invalid_mode_rejected_without_db_change(tmp_path):
    state, path = make_state(tmp_path)

    with state.lock:
        state.set_notify_mode("watch")

    with pytest.raises(ValueError):
        with state.lock:
            state.set_notify_mode("invalid")

    assert state.get_notify_mode() == "watch"

    restarted = BotState()
    restarted.enable_persistence(path)

    assert restarted.get_notify_mode() == "watch"


def test_failed_save_keeps_previous_mode(
    tmp_path, monkeypatch
):
    state, path = make_state(tmp_path)

    with state.lock:
        state.set_notify_mode("watch")

    def fail_save(db_path, mode):
        raise sqlite3.OperationalError(
            "database is locked"
        )

    monkeypatch.setattr(
        bot_state_module,
        "save_notify_mode",
        fail_save,
    )

    with pytest.raises(sqlite3.OperationalError):
        with state.lock:
            state.set_notify_mode("commands")

    assert state.get_notify_mode() == "watch"

    restarted = BotState()
    restarted.enable_persistence(path)

    assert restarted.get_notify_mode() == "watch"


def test_telegram_mode_command_persists(tmp_path):
    state, path = make_state(tmp_path)

    service = TelegramService(
        bot_token="fake_token",
        chat_id="123",
        sample_rate=16000,
        db_path=path,
        log=Mock(),
        state_lock=state.lock,
        waiting_for_correction=state.waiting_for_correction,
        get_notify_mode=state.get_notify_mode,
        set_notify_mode=state.set_notify_mode,
    )

    service.send_text = Mock()

    service.handle_update({
        "message": {
            "chat": {"id": 123},
            "text": "/commands",
        }
    })

    assert state.get_notify_mode() == "commands"
    service.send_text.assert_called_once()

    restarted = BotState()
    restarted.enable_persistence(path)

    assert restarted.get_notify_mode() == "commands"


def test_main_restores_mode_before_supervisor():
    source = (
        Path(__file__).resolve().parents[1] / "radio.py"
    ).read_text(encoding="utf-8")

    tree = ast.parse(source)

    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "main"
    )

    def call_name(node):
        if isinstance(node, ast.Expr):
            node = node.value
        elif isinstance(node, ast.Assign):
            node = node.value

        if not isinstance(node, ast.Call):
            return None

        func = node.func

        if isinstance(func, ast.Name):
            return func.id

        if isinstance(func, ast.Attribute):
            return func.attr

        return None

    calls = [
        (call_name(node), node.lineno)
        for node in main.body
    ]

    def line_of(name):
        matches = [
            lineno for found, lineno in calls
            if found == name
        ]
        assert len(matches) == 1, name
        return matches[0]

    assert (
        line_of("init_event_db")
        < line_of("load_dictionaries")
        < line_of("enable_persistence")
        < line_of("ApplicationSupervisor")
    )
