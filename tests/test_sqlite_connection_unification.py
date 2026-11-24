import inspect
from pathlib import Path

import pytest

import config
import shared.dictionary_store as database
import shared.event_store as event_store
import shared.sqlite_connection as shared


def test_modules_use_same_connection_manager():
    assert database.get_db is shared.get_db
    assert event_store.get_db is shared.get_db


def test_wal_timeout_and_commit(tmp_path):
    path = str(tmp_path / "events.db")

    with database.get_db(path) as db:
        journal = db.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]

        timeout = db.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]

        db.execute(
            "CREATE TABLE test_records (value TEXT)"
        )

        db.execute(
            "INSERT INTO test_records VALUES (?)",
            ("saved",),
        )

    assert journal.lower() == "wal"
    assert timeout == 10000

    # Другое подключение видит зафиксированные данные.
    with event_store.get_db(path) as db:
        rows = db.execute(
            "SELECT value FROM test_records"
        ).fetchall()

    assert rows == [("saved",)]


def test_rollback_preserves_database(tmp_path):
    path = str(tmp_path / "events.db")

    with database.get_db(path) as db:
        db.execute(
            "CREATE TABLE test_records (value TEXT)"
        )
        db.execute(
            "INSERT INTO test_records VALUES ('original')"
        )

    with pytest.raises(RuntimeError, match="rollback test"):
        with event_store.get_db(path) as db:
            db.execute(
                "INSERT INTO test_records VALUES ('failed')"
            )
            raise RuntimeError("rollback test")

    with database.get_db(path) as db:
        rows = db.execute(
            "SELECT value FROM test_records"
        ).fetchall()

    assert rows == [("original",)]


def test_default_paths_are_project_absolute(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    from shared.paths import place
    expected = str(place(Path(config.__file__).resolve().parent, "events.db"))

    assert config.DB_PATH == expected

    functions = (
        shared.get_db,
        database.init_db,
        database.import_dictionaries,
        event_store.init_db,
        event_store.save_correction,
        event_store.save_event,
    )

    # Проверяем сигнатуры, не открывая рабочую БД.
    for function in functions:
        actual = inspect.signature(
            function
        ).parameters["db_path"].default

        assert actual == expected, function.__name__

    # В постороннем каталоге ничего не создаётся.
    assert not (tmp_path / "events.db").exists()


def test_init_drops_the_unused_shared_notify_mode_table(tmp_path):
    import sqlite3
    import shared.dictionary_store as database
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bot_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        db.execute("INSERT INTO bot_settings VALUES ('notify_mode', 'off')")
    database.init_db(path)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='bot_settings'").fetchone()
