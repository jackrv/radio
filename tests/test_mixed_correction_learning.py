"""Навчання за змішаними правками оператора: слова, числа й позивні разом.

Лише тимчасові бази SQLite; Telegram не викликається.
"""

import sqlite3

import pytest

from shared.callsigns import Callsigns
from shared.word_learning import analyze_word_changes
from shared.word_rule_engine import apply_verified_word_rules
from shared.event_store import save_event
from shared.miniapp_correction_editor import save_edit_draft, decide_learning
from shared.management.common import ManagementConflict
from shared.management.learning import approve_rule, learning_queue
from services.management.overview import overview as management_overview
from shared.telegram_phrase_store import register_phrase
from shared.word_store import init_word_db, load_active_word_rules
from tests.test_miniapp_management import setup  # noqa: F401  (фікстура)
from tests.test_web_api import auth_header

ADMIN, VIEWER, EDITOR = 1001, 2002, 3003
DICTIONARY = ["Віра: 68 84", "Дуб: 12 14", "Маяк: 68 70 84"]


def seed_dictionary(path):
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', ?, ?)",
                       list(enumerate(DICTIONARY, 1)))


def dictionaries():
    empty = Callsigns.from_lines([])
    return (Callsigns.from_lines(DICTIONARY), empty, empty)


def event(path, text):
    return f"db:{save_event('', '', '', '', text, db_path=path, raw_text=text)}"


def phrase(path, token, text):
    register_phrase(path, token * 32, text)
    return "mem:" + token * 32


def votes(path):
    with sqlite3.connect(path) as db:
        return sorted(db.execute("SELECT source_id, original, corrected FROM word_correction_votes"))


def edit(path, fix_id, before, after, actor=EDITOR):
    return save_edit_draft(path, fix_id, before, after, actor_id=actor)


# ---------- Розбір правки ----------

def test_number_change_does_not_block_independent_word_fix():
    result = analyze_word_changes("Маяк 68, іду на пазіцію", "Маяк 84, іду на позицію")
    assert result.pairs == [("пазіцію", "позицію")]
    assert ("68 → 84", "number") in result.excluded


def test_only_numbers_changed_gives_no_candidates():
    result = analyze_word_changes("Маяк 68, прийом", "Маяк 84, прийом")
    assert result.pairs == [] and result.review == []
    assert result.excluded == [("68 → 84", "number")]


def test_number_words_and_digits_are_the_same_group():
    same = analyze_word_changes("Маяк шістдесят вісім, прийом", "Маяк 68, прийом")
    assert same.pairs == [] and same.excluded == [] and same.format_only
    changed = analyze_word_changes("Маяк сімдесят, прийом", "Маяк 68, прийом")
    assert changed.excluded == [("70 → 68", "number")] and changed.pairs == []


def test_long_mixed_correction_separates_words_numbers_and_callsigns():
    result = analyze_word_changes(
        "Вира 68, я Дуб 12, іду на пазіцію, прийом",
        "Віра 84, я Дуб 12, йду на позицію, прийом",
    )
    assert set(result.pairs) == {("іду", "йду"), ("пазіцію", "позицію")}
    # Назва змінена разом зі своїм номером — можливо, інший адресат: лише перевірка.
    assert result.review == [("вира", "віра", "with_number")]
    assert ("68 → 84", "number") in result.excluded


@pytest.mark.parametrize("before, after", [
    ("іду на позицію", "на позицію іду"),            # перестановка
    ("Маяк 68 іду", "Маяк 68 зараз іду"),             # вставка
    ("Маяк 68 зараз іду", "Маяк 68 іду"),             # видалення
    ("так так так", "так ні так"),                    # повтори
    ("нам не понятно зараз", "все ясно сейчас тут"),  # довге переписування
])
def test_insertions_permutations_repeats_and_rewrites_never_vote(before, after):
    assert analyze_word_changes(before, after).pairs == []


def test_inserted_word_never_enters_a_rule():
    """Помилка, знайдена при порівнянні з іншим патчем: «пазіцію» → «нашу позицію»."""
    from shared.word_store import _plan
    _, auto, review, excluded = _plan("Маяк 68, іду на пазіцію", "Маяк 84, іду на нашу позицію", frozenset())
    assert auto == {} and review == {("пазіцію", "нашу позицію"): "word_count"}
    _, auto, _, _ = _plan("на бат 110", "набат 110", frozenset())
    assert auto == {("на бат", "набат"): "word"}, "склеювання того самого написання лишається автоматичним"


def test_fix_left_elsewhere_in_phrase_needs_review():
    result = analyze_word_changes("пазіцію і пазіцію", "позицію і пазіцію")
    assert result.pairs == [] and result.review == [("пазіцію", "позицію", "leftover")]
    assert analyze_word_changes("пазіцію і пазіцію", "позицію і позицію").pairs == [("пазіцію", "позицію")]


def test_apostrophe_excludes_only_its_own_pair():
    result = analyze_word_changes("м'ята пазіція", "мята позиція")
    assert result.pairs == [("пазіція", "позиція")]
    assert result.excluded == [("м'ята → мята", "apostrophe")]


def test_one_word_fixed_two_ways_is_a_conflict():
    result = analyze_word_changes("пазіцію і пазіцію", "позицію і позіцію")
    assert result.pairs == [] and result.excluded[-1][1] == "conflict"


# ---------- Збереження, голоси й звіт ----------

def test_editor_report_shows_candidates_waiting_and_excluded(setup):
    _, path = setup
    seed_dictionary(path)
    fix = phrase(path, "a", "Маяк 68, іду на пазіцію")
    result = edit(path, fix, "Маяк 68, іду на пазіцію", "Маяк 84, іду на позицію")
    learning = result["learning"]
    assert result["saved"] and result["text_changed"]
    assert learning["candidates"] == [{"original": "пазіцію", "corrected": "позицію", "kind": "word",
                                       "confirmations": 1, "required": 2, "approved": False, "state": "waiting"}]
    assert [e["reason"] for e in learning["excluded"]] == ["number"]
    assert "номер" in learning["excluded"][0]["message"]
    assert votes(path) == [(fix, "пазіцію", "позицію")]


def test_callsign_swap_and_correct_names_are_never_candidates(setup):
    _, path = setup
    seed_dictionary(path)
    fix = event(path, "Віра 68, прийом")
    learning = edit(path, fix, "Віра 68, прийом", "Маяк 68, прийом")["learning"]
    assert learning["candidates"] == [] and learning["review"] == []
    assert learning["excluded"][0]["reason"] == "callsign_swap"
    fix = event(path, "Віра 68, прийом")
    learning = edit(path, fix, "Віра 68, прийом", "Вірка 68, прийом")["learning"]
    assert learning["excluded"][0]["reason"] == "known_name"
    assert votes(path) == []


def test_ambiguous_pairs_vote_only_after_operator_confirmation(setup):
    client, path = setup
    seed_dictionary(path)
    fix = event(path, "Вира 68, прийом")
    text = "Віра 84, прийом"
    learning = edit(path, fix, "Вира 68, прийом", text)["learning"]
    assert learning["candidates"] == []
    assert learning["review"] == [{"original": "вира", "corrected": "віра", "reason": "with_number",
                                   "message": learning["review"][0]["message"], "state": "pending"}]
    assert votes(path) == []

    body = {"fix_id": fix, "expected_text": text, "original": "вира", "corrected": "віра", "decision": "confirmed"}
    assert client.put("/api/corrections/learning", json=body, headers=auth_header(VIEWER)).status_code == 403
    assert votes(path) == []
    confirmed = client.put("/api/corrections/learning", json=body, headers=auth_header(EDITOR))
    assert confirmed.status_code == 200
    assert confirmed.json()["candidates"][0]["kind"] == "callsign"
    assert votes(path) == [(fix, "вира", "віра")]

    rejected = client.put("/api/corrections/learning", json={**body, "decision": "rejected"}, headers=auth_header(EDITOR))
    assert rejected.json()["review"][0]["state"] == "rejected" and votes(path) == []
    # Пара, якої немає в поточній правці, і застарілий текст.
    assert client.put("/api/corrections/learning", json={**body, "original": "дуб"}, headers=auth_header(EDITOR)).status_code == 404
    assert client.put("/api/corrections/learning", json={**body, "expected_text": "інше"}, headers=auth_header(EDITOR)).status_code == 409
    # Повторне відкриття показує збережене рішення.
    draft = client.get(f"/api/corrections/draft?fix_id={fix}", headers=auth_header(EDITOR)).json()
    assert draft["learning"]["review"][0]["state"] == "rejected"


def test_repeated_edit_of_one_source_replaces_votes(setup):
    _, path = setup
    seed_dictionary(path)
    fix = event(path, "Маяк 68, іду на пазіцію")
    edit(path, fix, "Маяк 68, іду на пазіцію", "Маяк 84, іду на позицію")
    edit(path, fix, "Маяк 84, іду на позицію", "Маяк 70, іду на позицію")
    assert votes(path) == [(fix, "пазіцію", "позицію")]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT confirmations FROM word_corrections").fetchall() == [(1,)]
    edit(path, fix, "Маяк 70, іду на позицію", "Маяк 70, іду на пазіцію")
    assert votes(path) == []


def test_saving_without_text_change(setup):
    _, path = setup
    fix = event(path, "Маяк 68, прийом")
    result = edit(path, fix, "Маяк 68, прийом", "Маяк 68, прийом")
    assert result["text_changed"] is False and result["learning"]["candidates"] == []
    assert votes(path) == []


def test_sqlite_error_rolls_back_text_votes_and_decisions(setup):
    _, path = setup
    seed_dictionary(path)
    fix = event(path, "Вира 68, іду на пазіцію")
    edit(path, fix, "Вира 68, іду на пазіцію", "Віра 84, іду на позицію")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_vote BEFORE INSERT ON word_correction_votes "
                   "BEGIN SELECT RAISE(ABORT, 'vote failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        decide_learning(path, fix, "Віра 84, іду на позицію", "вира", "віра", "confirmed", actor_id=EDITOR)
    with pytest.raises(sqlite3.IntegrityError):
        edit(path, fix, "Віра 84, іду на позицію", "Віра 84, іду на позиції")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM word_correction_reviews").fetchone()[0] == 0
        assert db.execute("SELECT text FROM radio_events").fetchone()[0] == "Віра 84, іду на позицію"
        assert db.execute("SELECT COUNT(*) FROM correction_sources").fetchone()[0] == 1
    assert votes(path) == [(fix, "пазіцію", "позицію")]


def test_db_and_mem_sources_give_the_same_report(setup):
    _, path = setup
    seed_dictionary(path)
    before, after = "Маяк 68, іду на пазіцію", "Маяк 84, іду на позицію"
    from_event = edit(path, event(path, before), before, after)["learning"]
    from_phrase = edit(path, phrase(path, "b", before), before, after)["learning"]
    assert from_event["excluded"] == from_phrase["excluded"]
    assert [c["original"] for c in from_event["candidates"]] == [c["original"] for c in from_phrase["candidates"]]
    assert from_phrase["candidates"][0]["confirmations"] == 2


def test_viewer_cannot_save_corrections(setup):
    client, path = setup
    fix = event(path, "Маяк 68, іду на пазіцію")
    body = {"fix_id": fix, "expected_text": "Маяк 68, іду на пазіцію", "corrected_text": "Маяк 84, іду на позицію"}
    assert client.put("/api/corrections/draft", json=body, headers=auth_header(VIEWER)).status_code == 403
    assert votes(path) == []


# ---------- Голоси з різних джерел, конфлікти, дозвіл ----------

def test_confirmations_from_sources_conflicts_and_approval(setup):
    client, path = setup
    seed_dictionary(path)
    for text in ("Маяк 68, іду на пазіцію", "Дуб 12, займаю пазіцію"):
        edit(path, event(path, text), text, text.replace("пазіцію", "позицію"))
    rule = learning_queue(path)["items"][0]
    assert (rule["original"], rule["corrected"], rule["confirmations"], rule["status"]) == ("пазіцію", "позицію", 2, "active")
    assert rule["kind"] == "word" and rule["needs_approval"] and not rule["applies"]
    assert apply_verified_word_rules("іду на пазіцію", path, dictionaries()) == "іду на пазіцію"

    with pytest.raises(PermissionError):
        approve_rule(path, VIEWER, rule["id"], True, rule["version"])
    with pytest.raises(ManagementConflict):
        approve_rule(path, EDITOR, rule["id"], True, "0" * 64)
    approve_rule(path, EDITOR, rule["id"], True, rule["version"])
    assert apply_verified_word_rules("іду на пазіцію", path, dictionaries()) == "іду на позицію"

    # Третє джерело пропонує інший варіант: конфлікт вимикає застосування.
    text = "Маяк 70, бачу пазіцію"
    edit(path, event(path, text), text, "Маяк 70, бачу позіцію")
    assert apply_verified_word_rules("іду на пазіцію", path, dictionaries()) == "іду на пазіцію"
    overview = management_overview(path)
    assert overview["learning"]["active_rules"] == 0 and overview["learning"]["candidates"] == 2
    assert overview["learning"]["confirmations"] == 3


def test_callsign_rules_need_no_approval_and_cannot_be_approved(setup):
    client, path = setup
    seed_dictionary(path)
    for before, after in (("Вира 68, я Дуб 12", "Віра 68, я Дуб 14"), ("Вира 84, прийом", "Віра 84, прийом")):
        edit(path, event(path, before), before, after)
    rule = learning_queue(path)["items"][0]
    assert rule["kind"] == "callsign" and rule["applies"] and not rule["needs_approval"]
    with pytest.raises(ValueError):
        approve_rule(path, EDITOR, rule["id"], True, rule["version"])


def test_migration_keeps_rules_and_never_activates_them_retroactively(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE word_corrections (id INTEGER PRIMARY KEY, original TEXT NOT NULL,
            corrected TEXT NOT NULL, confirmations INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(original, corrected), CHECK(original <> corrected))""")
        db.execute("INSERT INTO word_corrections (original, corrected, confirmations, status) VALUES ('пазіцію','позицію',1,'pending')")
    init_word_db(path)
    init_word_db(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT original, corrected, confirmations, status, approved_at FROM word_corrections").fetchall() \
            == [("пазіцію", "позицію", 1, "pending", None)]
    assert load_active_word_rules(path) == []


# ---------- Кінцевий результат ----------

def test_mixed_correction_trains_rule_that_fixes_next_message_keeping_its_numbers(setup):
    """Оператор виправляє змішані фрази; правило проходить підтвердження й дозвіл,
    а наступне повідомлення отримує виправлене слово зі своїми числами й позивними."""
    client, path = setup
    seed_dictionary(path)
    first = phrase(path, "c", "Маяк 68, іду на пазіцію")
    second = event(path, "Віра шістдесят вісім, я Дуб 12, займаю пазіцію")
    for fix, before, after in (
        (first, "Маяк 68, іду на пазіцію", "Маяк 84, іду на позицію"),
        (second, "Віра шістдесят вісім, я Дуб 12, займаю пазіцію", "Віра 84, я Дуб 14, займаю позицію"),
    ):
        response = client.put("/api/corrections/draft", headers=auth_header(EDITOR),
                              json={"fix_id": fix, "expected_text": before, "corrected_text": after})
        assert response.status_code == 200
        assert [c["original"] for c in response.json()["learning"]["candidates"]] == ["пазіцію"]

    rule = learning_queue(path)["items"][0]
    assert rule["status"] == "active" and rule["needs_approval"]
    approve_rule(path, ADMIN, rule["id"], True, rule["version"])

    from shared.pipeline import build_text_pipeline
    from shared.analyzer import correction_is_safe
    from unittest.mock import Mock

    def learned(text):
        revised = apply_verified_word_rules(text, path, dictionaries())
        return revised, revised != text

    process = build_text_pipeline(correct_with_llm=None, apply_learned_corrections=learned,
                                  dictionaries=dictionaries(), correction_is_safe=correction_is_safe, log=Mock())
    assert process("Маяк 70, я Віра 68, Дуб 12, тримаю пазіцію, прийом") \
        == "Маяк 70, я Віра 68, Дуб 12, тримаю позицію, прийом"
    assert process("Тримаю ПАЗІЦІЮ, прийом") == "Тримаю ПОЗИЦІЮ, прийом"
    assert process("пазіцію 84") == "пазіцію 84", "слово перед номером — місце позивного"
    assert process("пазіцію сто") == "пазіцію сто"
    assert process("опозиція пазіціями 68") == "опозиція пазіціями 68", "лише цілі слова"
