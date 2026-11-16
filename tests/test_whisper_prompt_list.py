"""Список «Підказка Whisper»: фрази перед назвами словників, живе оновлення, міграція."""

import sqlite3

import pytest

from database import init_db as init_dictionary_db
from services.text_lists import (
    LiveTextLists, compose_prompt, load_active, parse, read_lists, write_list,
)
from services.sqlite_connection import get_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "lists.db"
    init_dictionary_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES (?, ?, ?)",
                       [("callsigns", 1, "вишня: 15"), ("commands", 1, "злива 4242"), ("commands", 2, "грім 5858")])
    return path


def entries(*lines):
    return [{"raw_line": line, "enabled": True} for line in lines]


def save(path, *lines):
    return write_list(path, "whisper_prompt", entries(*lines), read_lists(path)["revision"])


def test_phrases_keep_case_and_digits():
    values, errors = parse("whisper_prompt", [(1, " Злива  42 42, "), (2, "грім 58 58."), (3, "Злива 42 42")])
    assert values == ["Злива 42 42", "грім 58 58"] and errors == []


def test_empty_long_and_oversized_phrases_are_rejected():
    _, errors = parse("whisper_prompt", [(1, " . "), (2, "а" * 81)])
    assert len(errors) == 2
    _, errors = parse("whisper_prompt", [(n, f"злива {n:02d} " + "б" * 70) for n in range(1, 6)])
    assert any("Разом" in e for e in errors)


def test_composed_prompt_matches_tested_form():
    assert compose_prompt(["Злива 42 42", "грім 58 58", "прийом"], "вишня, грім, злива") == \
        "Злива 42 42, грім 58 58, прийом. вишня, грім, злива"
    assert compose_prompt([], "вишня, злива") == "вишня, злива"


def test_saved_phrases_are_active_and_warn(db_path):
    result = save(db_path, "Злива 42 42", "Хмара 1 1")
    assert any("Хмара 1 1" in w for w in result["warnings"])
    assert any("хибні спрацьовування" in w for w in result["warnings"])
    with get_db(db_path) as db:
        _, values = load_active(db)
    assert values["whisper_prompt"] == ["Злива 42 42", "Хмара 1 1"]


def test_disabled_phrase_is_not_used(db_path):
    write_list(db_path, "whisper_prompt",
               [{"raw_line": "Злива 42 42", "enabled": False}, {"raw_line": "грім 58 58", "enabled": True}],
               read_lists(db_path)["revision"])
    with get_db(db_path) as db:
        _, values = load_active(db)
    assert values["whisper_prompt"] == ["грім 58 58"]


def test_live_lists_notify_listeners(db_path):
    lists = LiveTextLists(db_path, interval=0)
    calls = []
    lists.on_change(lambda: calls.append(lists.whisper_prompt))
    lists.refresh(force=True)
    save(db_path, "Злива 42 42")
    lists.refresh(force=True)
    assert calls == [(), ("Злива 42 42",)]


def test_old_table_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE text_list_entries (
            id INTEGER PRIMARY KEY,
            list_type TEXT NOT NULL CHECK(list_type IN ('blacklist', 'short_garbage', 'aliases', 'callsign_aliases')),
            line_number INTEGER NOT NULL, raw_line TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            UNIQUE(list_type, line_number))""")
        db.execute("CREATE TABLE text_list_state (id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL, "
                   "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO text_list_state (id, revision) VALUES (1, 7)")
        db.executemany("INSERT INTO text_list_entries (list_type, line_number, raw_line, enabled) VALUES (?, ?, ?, ?)",
                       [("blacklist", 1, "дякую за перегляд", 1), ("callsign_aliases", 1, "вишня: вишна", 0)])

    result = save(path, "Злива 42 42")

    assert result["revision"] == 8
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT list_type, raw_line, enabled FROM text_list_entries ORDER BY id").fetchall()
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='text_list_entries'").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert rows == [("blacklist", "дякую за перегляд", 1), ("callsign_aliases", "вишня: вишна", 0),
                    ("whisper_prompt", "Злива 42 42", 1)]
    assert "'whisper_prompt'" in sql and "text_list_entries_new" not in tables
