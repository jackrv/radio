"""Шаблони зворотів у SQLite: редагування, живе застосування й нові правила шаблонів."""
import sqlite3

import pytest

from core.callsigns import Callsigns
import core.template_text as tt
from core.template_text import join_split_callsign, template_text, trim_loops
from services.dictionary_store import init_db
from services.text_lists import check_list, ensure_tables, load_active, read_lists, sync_aliases, write_list


@pytest.fixture(autouse=True)
def restore_phrases():
    yield
    tt.configure_phrases(tt.DEFAULT_PHRASES)


def test_templates_are_seeded_once_edited_and_applied_live(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    data = read_lists(path)
    lines = [e["raw_line"] for e in data["lists"]["phrase_templates"]["entries"]]
    assert "посилку отримав" in lines and "як прийнято" in lines
    write_list(path, "phrase_templates", [{"raw_line": "до роботи приступив", "enabled": True}], data["revision"])
    with sqlite3.connect(path) as db:
        ensure_tables(db)                     # повторно не дописує початкові значення
        _, values = load_active(db)
        assert values["phrase_templates"] == ["до роботи приступив"]
        sync_aliases(db)
    assert tt.PHRASES == ("до роботи приступив",)
    assert template_text("Посилку отримав, прийом", Callsigns.from_lines(["клен: 5"])) == "Посилку отримав, прийом"


def test_template_lines_are_validated_without_saving(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 1, 'Клен: 5')")
    bad = check_list(path, "phrase_templates", [{"raw_line": "клен прийняв", "enabled": True}, {"raw_line": "код 42", "enabled": True}])
    assert any("позивний «клен»" in e for e in bad["errors"]) and any("чисел" in e for e in bad["errors"])
    ok = check_list(path, "phrase_templates", [{"raw_line": "На п'ятірку", "enabled": True}])
    assert ok["errors"] == []


def test_split_callsign_is_joined_only_at_callsign_position_with_its_number():
    callsigns = Callsigns.from_lines(["дуб: 100-114", "клен: 700"])
    assert join_split_callsign("на 109, на 109, я клен 700", callsigns) == "на 109, на 109, я клен 700", "лише позивні на «на…»"
    assert join_split_callsign("на 109, прийом", Callsigns.from_lines(["набат: 100-114"])) == "набат 109, прийом"
    assert join_split_callsign("Я на 112, прийом", Callsigns.from_lines(["набат: 100-114"])) == "Я набат 112, прийом"
    assert join_split_callsign("їду на 109 кілометр", Callsigns.from_lines(["набат: 100-114"])) == "їду на 109 кілометр"
    assert join_split_callsign("на 117, прийом", Callsigns.from_lines(["набат: 100-114"])) == "на 117, прийом", "номер не підбирається"


def test_whisper_loops_are_trimmed_but_service_words_are_kept():
    assert trim_loops("вишнею, вишнею, вишнею, вишнею, вишнею") == "вишнею, вишнею"
    assert trim_loops("Клен, клен, клен, клен 700") == "Клен, клен 700"
    assert trim_loops("Прийом. Прийом. Прийом. Прийом.") == "Прийом. Прийом. Прийом. Прийом."
    assert trim_loops("клен 5, клен 5, прийом") == "клен 5, клен 5, прийом"
