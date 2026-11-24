import sqlite3
from unittest.mock import Mock

import pytest

from shared.dictionary_store import init_db
from shared.dictionary_loader import load_dictionaries


def prepare_database(tmp_path, entries=()):
    path = str(tmp_path / "events.db")
    init_db(path)

    with sqlite3.connect(path) as db:
        db.executemany(
            """
            INSERT INTO dictionary_entries
                (dictionary_type, line_number, raw_line)
            VALUES (?, ?, ?)
            """,
            entries,
        )

    return path


def test_empty_database_stops_startup(tmp_path):
    path = prepare_database(tmp_path)
    log = Mock()

    with pytest.raises(RuntimeError, match="Словник позивних порожній"):
        load_dictionaries(path, log)

    log.error.assert_called_once()


def test_comment_only_callsigns_are_empty(tmp_path):
    path = prepare_database(
        tmp_path,
        [
            ("callsigns", 1, "# комментарий"),
            ("callsigns", 2, "   "),
        ],
    )

    with pytest.raises(RuntimeError, match="Словник позивних порожній"):
        load_dictionaries(path, Mock())


def test_optional_empty_dictionaries_warn(tmp_path):
    path = prepare_database(
        tmp_path,
        [("callsigns", 1, "вишня: 700")],
    )

    log = Mock()

    callsigns, commands, watch, prompt = load_dictionaries(
        path, log
    )

    assert "вишня" in callsigns.names
    assert not commands.names
    assert not watch.names
    assert "вишня" in prompt
    assert log.warning.call_count == 2


def test_populated_dictionaries_load(tmp_path):
    path = prepare_database(
        tmp_path,
        [
            ("callsigns", 1, "вишня: 700"),
            ("commands", 1, "прийом"),
            ("telegram", 1, "туча: 042"),
        ],
    )

    log = Mock()

    callsigns, commands, watch, prompt = load_dictionaries(
        path, log
    )

    assert "вишня" in callsigns.names
    assert "прийом" in commands.names
    assert "туча" in watch.names

    assert all(
        name in prompt
        for name in ("вишня", "прийом", "туча")
    )

    log.warning.assert_not_called()
    log.error.assert_not_called()
