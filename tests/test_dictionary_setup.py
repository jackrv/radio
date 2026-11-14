"""Перевірка словників перед запуском: лише тимчасові бази й dictionaries.txt."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import start_radio345 as launcher
from database import init_db
from services.database_lifecycle import initialize_database
from services.dictionary_setup import dictionary_status, ensure_dictionaries, has_active_admin
from services.sqlite_connection import get_db
from services.user_store import init_users_db


def write_sources(folder, callsigns="липка: 700\n"):
    path = folder / "dictionaries.txt"
    path.write_text(
        f"[callsigns]\n{callsigns}\n[commands]\nприйом\n\n[telegram]\nлипка\n",
        encoding="utf-8",
    )
    return path


def real_init(command, cwd):
    """Виконує справжній manage_database.py init поточним інтерпретатором."""
    return subprocess.run([sys.executable, *command[1:]], cwd=cwd, capture_output=True)


def ensure(tmp_path, *, answer="y", interactive=True, run=real_init):
    messages = []
    ask = Mock(return_value=answer)
    ok = ensure_dictionaries(
        tmp_path / "events.db", tmp_path / "dictionaries.txt", sys.executable,
        interactive=interactive, ask=ask, out=messages.append, run=run,
    )
    return ok, ask, messages


def test_status_does_not_create_missing_database(tmp_path):
    assert dictionary_status(tmp_path / "events.db") is None
    assert not (tmp_path / "events.db").exists()


def test_filled_dictionaries_pass_without_questions(tmp_path):
    write_sources(tmp_path)
    initialize_database(tmp_path / "events.db", tmp_path / "dictionaries.txt")
    run = Mock()
    ok, ask, messages = ensure(tmp_path, run=run)
    assert ok is True
    ask.assert_not_called()
    run.assert_not_called()
    assert messages == []


def test_empty_database_imports_after_confirmation(tmp_path):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path)
    ok, ask, messages = ensure(tmp_path, answer=" YES ")
    assert ok is True
    ask.assert_called_once()
    assert dictionary_status(tmp_path / "events.db")["callsigns"] == (1, 1)
    assert (tmp_path / "dictionaries.txt").exists(), "файл не видаляється без явного прапорця"
    assert "липка" not in "\n".join(messages), "вміст словників не виводиться"


def test_missing_database_is_created_only_after_confirmation(tmp_path):
    write_sources(tmp_path)
    ok, _, _ = ensure(tmp_path, answer="n")
    assert ok is False
    assert not (tmp_path / "events.db").exists()
    ok, _, _ = ensure(tmp_path, answer="Y")
    assert ok is True


@pytest.mark.parametrize("answer", ["", "n", "N", "no", "т", "так", "н"])
def test_refusal_imports_nothing(tmp_path, answer):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path)
    run = Mock()
    ok, _, messages = ensure(tmp_path, answer=answer, run=run)
    assert ok is False
    run.assert_not_called()
    assert any("скасовано" in line for line in messages)


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_interrupted_prompt_is_refusal(tmp_path, error):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path)
    run = Mock()
    ok = ensure_dictionaries(
        tmp_path / "events.db", tmp_path / "dictionaries.txt", sys.executable, interactive=True,
        ask=Mock(side_effect=error), out=lambda _: None, run=run,
    )
    assert ok is False
    run.assert_not_called()


def test_non_interactive_console_only_explains(tmp_path):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path)
    run = Mock()
    ok, ask, messages = ensure(tmp_path, interactive=False, run=run)
    assert ok is False
    ask.assert_not_called()
    run.assert_not_called()
    assert any("manage_database.py init" in line for line in messages)


def test_missing_source_is_reported(tmp_path):
    init_db(tmp_path / "events.db")
    ok, ask, messages = ensure(tmp_path)
    assert ok is False
    ask.assert_not_called()
    assert any("dictionaries.example.txt" in line for line in messages)


def test_broken_source_is_reported_without_prompt(tmp_path):
    init_db(tmp_path / "events.db")
    (tmp_path / "dictionaries.txt").write_text("липка: 700\n", encoding="utf-8")
    ok, ask, messages = ensure(tmp_path)
    assert ok is False
    ask.assert_not_called()
    assert any("поза розділом" in line for line in messages)


def test_comment_only_callsigns_are_not_offered(tmp_path):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path, callsigns="# лише коментар\n\n")
    ok, ask, _ = ensure(tmp_path)
    assert ok is False
    ask.assert_not_called()


def test_partially_filled_database_is_not_reimported(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    with get_db(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('commands', 1, 'прийом')")
    write_sources(tmp_path)
    run = Mock()
    ok, ask, _ = ensure(tmp_path, run=run)
    assert ok is False
    ask.assert_not_called()
    run.assert_not_called()


def test_failed_import_stops_start(tmp_path):
    init_db(tmp_path / "events.db")
    write_sources(tmp_path)
    ok, _, messages = ensure(tmp_path, run=lambda *a, **k: SimpleNamespace(returncode=1))
    assert ok is False
    assert any("не виконано" in line for line in messages)


def test_active_admin_detection(tmp_path):
    path = tmp_path / "events.db"
    assert has_active_admin(path) is False
    init_users_db(path)
    assert has_active_admin(path) is False
    with get_db(path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role, enabled) VALUES (1, 'admin', 1)")
    assert has_active_admin(path) is True


def test_launcher_stops_before_services_without_dictionaries(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "api").mkdir()
    for name in ["radio.py", "api/local_server.py", ".env"]:
        (tmp_path / name).touch()
    monkeypatch.setattr(sys, "argv", ["start_radio345.py"])
    monkeypatch.setattr(launcher, "launcher_setting", lambda *args: "")
    monkeypatch.setattr(launcher, "interactive_console", lambda: False)
    started = Mock()
    monkeypatch.setattr(launcher, "ManagedProcess", started)
    assert launcher.main() == 2
    started.assert_not_called()
    assert not (tmp_path / "events.db").exists()


def test_launcher_prompt_has_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "api").mkdir()
    for name in ["radio.py", "api/local_server.py", ".env"]:
        (tmp_path / name).touch()
    write_sources(tmp_path)
    monkeypatch.setattr(sys, "argv", ["start_radio345.py"])
    monkeypatch.setattr(launcher, "launcher_setting", lambda *args: "")
    monkeypatch.setattr(launcher, "interactive_console", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda text: prompts.append(text) or "n")
    started = Mock()
    monkeypatch.setattr(launcher, "ManagedProcess", started)
    assert launcher.main() == 2
    assert prompts == ["[launcher]   Імпортувати словники з dictionaries.txt у базу зараз? [Y/N]: "]
    started.assert_not_called()
