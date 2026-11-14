"""Формат єдиного файлу словників dictionaries.txt."""

from pathlib import Path

import pytest

from database import format_dictionary_file, parse_dictionary_file

ROOT = Path(__file__).resolve().parent.parent


def test_sections_comments_and_disabled_entries():
    text = """# шапка
[callsigns]
липка: 700 701   
# коментар усередині розділу

# @radio345-disabled: беркут
[Commands]
буревій 1313
[telegram]
"""
    assert parse_dictionary_file(text) == {
        "callsigns": [("липка: 700 701", True), ("беркут", False)],
        "commands": [("буревій 1313", True)],
        "telegram": [],
    }


def test_missing_sections_are_empty():
    assert parse_dictionary_file("[callsigns]\nлипка\n") == {
        "callsigns": [("липка", True)], "commands": [], "telegram": [],
    }


@pytest.mark.parametrize(("text", "message"), [
    ("липка: 700\n", "поза розділом"),
    ("[позивні]\nлипка\n", "невідомий розділ"),
    ("[callsigns]\nлипка\n[callsigns]\nнаган\n", "повторюється"),
])
def test_invalid_structure_names_the_line(text, message):
    with pytest.raises(ValueError, match=message):
        parse_dictionary_file(text)


def test_format_round_trip_keeps_order_and_disabled_state():
    entries = {
        "callsigns": [("липка: 700", True), ("наган: 100-114", False)],
        "commands": [],
        "telegram": [("ліра: 68", True)],
    }
    assert parse_dictionary_file(format_dictionary_file(entries)) == entries


def test_shipped_template_has_all_sections_but_no_entries():
    text = (ROOT / "dictionaries.example.txt").read_text(encoding="utf-8")
    for section in ("[callsigns]", "[commands]", "[telegram]"):
        assert section in text
    assert parse_dictionary_file(text) == {"callsigns": [], "commands": [], "telegram": []}
