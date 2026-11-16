"""Перевірка оформлення тексту в редакторах: лише підказки, нічого не зберігає."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from callsigns import Callsigns, command_dictionary
from database import init_db as init_dictionary_db
from services.event_store import init_db as init_event_db
from services.text_check import check_text, format_text, tidy_text
from services.user_store import bootstrap_admin, create_user, init_users_db
from tests.test_web_api import TOKEN, auth_header

CALLSIGNS = Callsigns.from_lines(["береза: 31 56", "клен: 15", "вишня"])
COMMANDS = command_dictionary(["злива 4242", "грім 022"])


def check(text):
    return check_text(text, CALLSIGNS, COMMANDS, None)


def kinds(result):
    return [issue["kind"] for issue in result["issues"]]


def test_clean_text_has_no_remarks():
    assert check("Береза 31, Береза 31, я Клен 15, злива 42 42, грім 0 22, на звʼязок, прийом") == {
        "issues": [], "formatted": None}


def test_format_rules_and_fix_all():
    text = "Береза,31  я клен 15 , злива 4242, грім 022, 96,80, с півночі, на звязок"
    result = check(text)
    assert set(kinds(result)) == {"format"}
    assert format_text(text) == "Береза, 31 я клен 15, злива 42 42, грім 0 22, 96 80, з півночі, на звʼязок"
    assert result["formatted"] == "Береза 31, я Клен 15, злива 42 42, грім 0 22, 96 80, з півночі, на звʼязок"


def test_each_fix_changes_only_its_place():
    result = check("Злива 4242, С півночі")
    fixed = {issue["fixed_text"] for issue in result["issues"]}
    assert fixed == {"Злива 42 42, С півночі", "Злива 4242, З півночі"}


def test_callsign_numbers_are_not_touched():
    assert check("Береза 56, Клен 15, Береза 700")["formatted"] is None


def test_spelling_suggestions_come_from_dictionary_and_templates():
    result = check("Бериза 56, я Клен 15, до роботи переступив, прийом")
    assert [(i["kind"], i["fixed_text"]) for i in result["issues"]] == [
        ("spelling", "Береза 56, я Клен 15, до роботи переступив, прийом"),
        ("spelling", "Бериза 56, я Клен 15, до роботи приступив, прийом"),
    ]
    assert result["formatted"] is None, "опечатки не виправляються «усім оформленням»"


def test_dictionary_name_spelled_with_other_vowel_letter():
    result = check("Клєн 15, прийом")
    assert result["issues"][0]["fixed_text"] == "Клен 15, прийом"


def test_number_missing_from_dictionary_is_only_a_note():
    notes = [i for i in check("береза 99, злива 27 27, грім 0 22")["issues"] if i["kind"] == "info"]
    assert len(notes) == 2 and not any("fixed_text" in i for i in notes)
    assert "«береза 99»" in notes[0]["message"] and "«злива 27»" in notes[1]["message"]


def test_alias_is_not_applied_to_a_dictionary_word(monkeypatch):
    import callsigns
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"злива": "береза", "бук": "клен"})
    notes = [i["message"] for i in check("злива 42 42, бук 99")["issues"] if i["kind"] == "info"]
    assert len(notes) == 1 and "«клен 99»" in notes[0]


def test_russian_letter_is_a_note():
    assert [i["kind"] for i in check("Клен 15, приём")["issues"]] == ["spelling", "info"]


@pytest.fixture
def api(tmp_path):
    db = tmp_path / "events.db"
    init_event_db(db)
    init_dictionary_db(db)
    init_users_db(db)
    bootstrap_admin(db, 1001)
    create_user(db, 1001, 2002, role="viewer")
    create_user(db, 1001, 3003, role="editor")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM dictionary_entries")
        conn.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES (?,?,?)",
                         [("callsigns", 1, "береза: 31 56"), ("commands", 1, "злива 4242")])
    with TestClient(create_app(db, TOKEN)) as client:
        yield db, client


def test_miniapp_check_endpoint_uses_database_dictionaries_and_permissions(api):
    _, client = api
    response = client.post("/api/corrections/check", headers=auth_header(3003), json={"text": "Бериза 56, Береза 99, злива 4242"})
    assert response.status_code == 200
    body = response.json()
    assert body["formatted"] == "Бериза 56, Береза 99, злива 42 42"
    assert {i["kind"] for i in body["issues"]} == {"format", "spelling", "info"}
    assert client.post("/api/corrections/check", headers=auth_header(2002), json={"text": "x"}).status_code == 403
    assert client.post("/api/corrections/check", headers=auth_header(3003), json={"text": "x" * 5001}).status_code == 422


@pytest.mark.parametrize("text, expected", [
    ("береза 700 береза 700 я береза 56 на звʼязок прийом", "Береза 700, Береза 700, я Береза 56, на звʼязок, прийом"),
    ("клен 15 посилку отримав прийом | клен 15 прийнято прийом", "Клен 15, посилку отримав, прийом | Клен 15, прийнято, прийом"),
    ("Злива 42 42, злива 42 42, я береза 700, грім, 0 22, прийом", "Злива 42 42, злива 42 42, я Береза 700, грім 0 22, прийом"),
    ("Клен, 15, 26 84, Прийом.", "Клен 15, 26 84, прийом."),
    ("вишня 96, 80, прийом", "Вишня, 96, 80, прийом"),
    ("822 прийнято прийом", "822, прийнято, прийом"),
    ("береза 700 перевірка звʼязку прийом", "Береза 700, перевірка звʼязку, прийом"),   # команда без номера — слово
    ("береза 700, балістика з півночі, негайно в укриття", "Береза 700, балістика з півночі, негайно в укриття"),
    ("Бериза 56, прийом", "Бериза 56, прийом"),            # слово з опечаткою: кома перед номером не додається
])
def test_tidy_puts_commas_between_groups_and_capitalizes_callsigns(text, expected):
    assert tidy_text(text, CALLSIGNS, COMMANDS) == expected


def test_tidy_never_changes_words_and_is_stable():
    for text in ("береза 700 я клен 15 прийнято прийом", "Клен 15, 17 86, 17 86, прийом", "прийом"):
        tidy = tidy_text(text, CALLSIGNS, COMMANDS)
        assert tidy.lower().replace(",", "").split() == text.lower().replace(",", "").split()
        assert tidy_text(tidy, CALLSIGNS, COMMANDS) == tidy


def test_tidy_is_offered_as_one_formatting_fix():
    result = check("береза 56 посилку отримав прийом")
    assert [(i["kind"], i["message"]) for i in result["issues"]] == [("format", "Коми між групами й великі літери позивних")]
    assert result["formatted"] == "Береза 56, посилку отримав, прийом"
