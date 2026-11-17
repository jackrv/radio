"""Підказка лабораторії: власні фрази й коди доби конкретного запису."""
import sqlite3

from services.dictionary_store import init_db
from services import daily_codes
from tools.asr_lab import receiver_prompt, record_codes


def test_phrases_and_record_codes_go_before_dictionary_names(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    daily_codes.write_permanent(path, "31 05")
    daily_codes.write(path, "2026-11-17", "48 62")
    created = __import__("datetime").datetime(2026, 11, 16, 21, 0).timestamp()   # після 20:00 — доба 17.11
    codes = record_codes(path, created)
    assert codes == ["31 05", "48 62"]
    assert receiver_prompt(path, "клен, дуб", ["злива 42, 42"], codes) == "злива 42, 42, 31 05, 48 62. клен, дуб"


def test_latest_mode_takes_only_the_last_code_of_the_record_day(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    daily_codes.write_permanent(path, "31 05")
    daily_codes.write(path, "2026-11-17", "26 42, 48 62")
    created = __import__("datetime").datetime(2026, 11, 17, 10, 0).timestamp()
    assert record_codes(path, created, "latest") == ["48 62"]
