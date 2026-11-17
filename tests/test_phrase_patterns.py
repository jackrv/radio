"""Шаблони цілих фраз: лише виправлення написання, без вигадок і без зміни чисел."""
import sqlite3

import pytest

from core.callsigns import Callsigns
import core.phrase_patterns as pp
from core.phrase_patterns import apply_patterns, parse_pattern
from services.dictionary_store import init_db
from services.text_lists import load_active, read_lists

CALLSIGNS = Callsigns.from_lines(["калина: 700 936", "набат: 100-114", "дуб", "ліна: 13 63"])
COMMANDS = ("перевірка",)


def patterns(*lines):
    return [(line, parse_pattern(line)) for line in lines]


@pytest.fixture(autouse=True)
def no_live_patterns():
    pp.configure_patterns(())
    yield
    pp.configure_patterns(())


def test_pattern_syntax_is_validated():
    assert parse_pattern("{П}, я {П}, прийом") == [("P",), ("W", "я"), ("P",), ("W", "прийом")]
    for bad in ("{П} {Х}", "{П}, 42, прийом", "{П}, {П}"):
        with pytest.raises(ValueError):
            parse_pattern(bad)


def test_whole_phrase_is_respelled_and_slot_word_becomes_callsign_with_its_number():
    p = patterns("{П}, {П}, я {П}, {К} звʼязку, прийом", "{П}, прийнято, прийом")
    text = "Набат 109, набат 109, я Колина 700, перевірка звʼязкю, прийом. ліна 63, приняте, прийом"
    assert apply_patterns(text, CALLSIGNS, p, known_names=COMMANDS) == \
        "Набат 109, набат 109, я Калина 700, перевірка звʼязку, прийом. ліна 63, прийнято, прийом"


def test_numbers_never_change_and_unknown_numbers_keep_the_heard_word():
    p = patterns("{П}, прийнято, прийом")
    assert apply_patterns("Колина 701, приняте, прийом", CALLSIGNS, p) == "Колина 701, прийнято, прийом", \
        "номера 701 немає в словнику — слово позивного не змінюється, число теж"


def test_without_number_only_near_exact_callsign_is_used():
    p = patterns("{П}, {П}, прийнято, прийом")
    assert apply_patterns("калина 700, берег, прийнято, прийом", CALLSIGNS, p) == "калина 700, берег, прийнято, прийом"
    assert apply_patterns("калина 700, дубб, прийнято, прийом", CALLSIGNS, p) == "калина 700, дуб, прийнято, прийом"


def test_correct_service_words_and_dictionary_names_are_not_rewritten():
    p = patterns("{П}, прийнято, прийом", "{П}, {П}, прийнято, прийом")
    assert apply_patterns("калина 700, прийом, прийом", CALLSIGNS, p) == "калина 700, прийом, прийом"
    assert apply_patterns("калина 700, дуб, прийом", CALLSIGNS, p) == "калина 700, дуб, прийом"


def test_command_slot_needs_a_command_and_extra_words_reject_the_pattern():
    p = patterns("{П}, {П}, я {П}, {К} звʼязку, прийом")
    text = "калина 700, калина 700, я ліна 63, на звʼязок, прийом"
    assert apply_patterns(text, CALLSIGNS, p, known_names=COMMANDS) == text, "«на» — не команда"
    assert apply_patterns("Їду на роботу, все добре", CALLSIGNS, patterns("{П}, прийом")) == "Їду на роботу, все добре"


def test_candidate_patterns_are_seeded_disabled_and_do_not_apply_until_enabled(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    entries = read_lists(path)["lists"]["phrase_patterns"]["entries"]
    assert entries and not any(e["enabled"] for e in entries)
    with sqlite3.connect(path) as db:
        _, values = load_active(db)
    assert values["phrase_patterns"] == []
