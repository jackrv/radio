"""Текстові списки в SQLite: початкові значення, перевірки, застосування без перезапуску."""

import sqlite3
from unittest.mock import Mock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import core.callsigns as callsign_module
import services.text_lists as text_lists
from core.callsigns import Callsigns, configure_aliases
from core.transcriber import Transcriber
from services.dictionary_store import init_db as init_dictionary_db
from services.text_lists import LiveTextLists, read_lists, write_list, TextListConflict

ORIGIN = "http://127.0.0.1:8766"
KEY = "x" * 43


@pytest.fixture(autouse=True)
def restore_aliases():
    yield
    configure_aliases(callsign_module.DEFAULT_ALIASES, callsign_module.DEFAULT_CALLSIGN_ALIASES)
    text_lists._synced = None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "lists.db"
    init_dictionary_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES (?, ?, ?)",
                       [("callsigns", 1, "калина: 700"), ("callsigns", 2, "віра: 68"), ("commands", 1, "відхід")])
    return path


def entries(*lines):
    return [{"raw_line": line, "enabled": True} for line in lines]


def save(path, list_type, *lines):
    return write_list(path, list_type, entries(*lines), read_lists(path)["revision"])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_defaults_from_code_are_seeded_once(db_path):
    data = read_lists(db_path)
    lists = {k: [e["raw_line"] for e in v["entries"]] for k, v in data["lists"].items()}
    assert "дякую за перегляд" in lists["blacklist"]
    assert "па па" in lists["short_garbage"]
    assert "набат: на бат, бат, на bat, на бату" in lists["aliases"]
    assert lists["callsign_aliases"] == ["калина: калиниця"]
    assert data["revision"] == 1
    write_list(db_path, "short_garbage", [], 1)
    assert read_lists(db_path)["lists"]["short_garbage"]["entries"] == [], "порожній список не заповнюється знову"


@pytest.mark.parametrize("list_type, line, message", [
    ("blacklist", "калина на зв'язку", "словникове ім’я"),
    ("blacklist", "дякую 68", "з числом"),
    ("short_garbage", "відхід", "словникове ім’я"),
    ("aliases", "береза: віра", "правильне словникове ім’я"),
    ("aliases", "береза береза", "формат"),
    ("aliases", "вісім: вістим", "числа"),
    ("callsign_aliases", "калина: віра", "правильне словникове ім’я"),
    ("callsign_aliases", "калина: мала каліна", "по одному слову"),
])
def test_dangerous_lines_are_rejected(db_path, list_type, line, message):
    with pytest.raises(ValueError, match=message):
        save(db_path, list_type, line)


def test_variant_cannot_lead_to_two_names_and_stale_revision_conflicts(db_path):
    with pytest.raises(ValueError, match="уже веде"):
        save(db_path, "aliases", "набат: бат", "вовк: бат")
    write_list(db_path, "blacklist", entries("перша"), 1)
    with pytest.raises(TextListConflict):
        write_list(db_path, "blacklist", entries("друга"), 1)


def test_unknown_callsign_alias_only_warns(db_path):
    result = save(db_path, "callsign_aliases", "береза: берізка")
    assert result["saved"] and "немає у словнику" in result["warnings"][0]


def test_other_dictionary_word_needs_numbered_callsign_without_shared_numbers(db_path):
    with sqlite3.connect(db_path) as db:
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES (?, ?, ?)",
                       [("callsigns", 3, "дуб"), ("callsigns", 4, "вишня: 210"), ("commands", 2, "злива 222"), ("commands", 3, "грім 210")])
    result = save(db_path, "callsign_aliases", "вишня: злива")
    assert result["saved"] and "лише перед номерами «вишня»" in result["warnings"][0]
    with pytest.raises(ValueError, match="ті самі номери, що й «вишня»: 210"):
        save(db_path, "callsign_aliases", "вишня: грім")
    with pytest.raises(ValueError, match="переписував би це слово скрізь"):
        save(db_path, "callsign_aliases", "дуб: злива")
    standalone = save(db_path, "callsign_aliases", "дуб: дубок")
    assert "у будь-якій фразі" in standalone["warnings"][0]


def test_disabled_lines_are_kept_but_not_applied(db_path):
    write_list(db_path, "blacklist", [{"raw_line": "стара фраза", "enabled": False},
                                      {"raw_line": "нова фраза", "enabled": True}], 1)
    live = LiveTextLists(db_path)
    live.refresh(force=True)
    assert live.blacklist == {"нова фраза"}
    assert read_lists(db_path)["lists"]["blacklist"]["entries"][0] == {"raw_line": "стара фраза", "enabled": False}


def make_worker(text, live):
    recognizer = Mock()
    recognizer.extract_text.return_value = text
    processor = Mock()
    worker = Transcriber(segments=Mock(), recognizer=recognizer, process_text=lambda v: v, event_processor=processor,
                         sample_rate=16000, min_rms=0.02, normalize_audio=True, debug_rms=False, blacklist=(),
                         log=Mock(), text_lists=live)
    return worker, processor


def loud():
    return np.full(16000, 10000, dtype=np.int16).tobytes()


def test_blacklist_and_aliases_apply_to_next_segment_without_restart(db_path):
    clock = Clock()
    live = LiveTextLists(db_path, interval=2.0, clock=clock)
    worker, processor = make_worker("Нова галюцинація!", live)
    worker.process_segment(loud())
    assert processor.process.call_count == 1

    save(db_path, "blacklist", "нова галюцинація")
    clock.now = 1.0
    worker.process_segment(loud())
    assert processor.process.call_count == 2, "ревізію перевіряють не частіше ніж раз на 2 с"
    clock.now = 3.5
    worker.process_segment(loud())
    assert processor.process.call_count == 2, "фразу відкинуто без перезапуску"

    dictionary = Callsigns.from_lines(["калина: 700"])
    assert dictionary.find("смайлик 700") == []
    save(db_path, "callsign_aliases", "калина: смайлик")
    clock.now = 6.0
    worker.process_segment(loud())
    assert dictionary.find("смайлик 700") == [("калина", "700", 100)]
    assert dictionary.find("калиниця 700") == [], "старий алиас прибрано"


def test_short_garbage_view_follows_live_lists(db_path):
    from core.analyzer import is_short_garbage
    live = LiveTextLists(db_path)
    live.refresh(force=True)
    view = live.short_garbage_view()
    assert is_short_garbage("Па-па!", view)
    save(db_path, "short_garbage", "ой")
    live.refresh(force=True)
    assert not is_short_garbage("па па", view) and is_short_garbage("Ой", view)


def test_database_error_keeps_previous_lists(db_path):
    log = Mock()
    live = LiveTextLists(db_path, log=log)
    live.refresh(force=True)
    before = live.blacklist
    with patch("services.text_lists.get_db", side_effect=sqlite3.OperationalError("locked")):
        assert live.refresh(force=True) is False
    assert live.blacklist == before and log.warning.called


def test_editor_correction_uses_current_callsign_aliases(db_path):
    from services.event_store import init_db, save_event, save_correction
    init_db(db_path)
    save(db_path, "callsign_aliases", "калина: смайлик")
    event_id = save_event("", "", "", "", "Смайлик 700", db_path=db_path, raw_text="Смайлик 700")
    save_correction("Смайлик 700", "Смайлик 700, прийом", event_id, db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT callsigns FROM radio_events WHERE id = ?", (event_id,)).fetchone()[0] == "калина 700"


def test_local_panel_reads_and_saves_lists(tmp_path):
    from services.event_store import init_db
    from services.user_store import init_users_db, bootstrap_admin
    from api.local_dashboard import create_dashboard
    path = tmp_path / "panel.db"
    init_db(path); init_dictionary_db(path); init_users_db(path); bootstrap_admin(path, 1001)
    app = create_dashboard(path, KEY, samples_dir=tmp_path / "s", diagnostics_path=tmp_path / "d.json", recordings_dir=tmp_path / "r")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1)) as client:
        client.post("/session", json={"token": KEY}, headers={"Origin": ORIGIN})
        data = client.get("/api/text-lists").json()
        assert set(data["lists"]) == {"blacklist", "short_garbage", "aliases", "callsign_aliases", "whisper_prompt", "phrase_templates", "phrase_patterns"}
        body = {"entries": [{"raw_line": "субтитри зроблено", "enabled": True}], "expected_revision": data["revision"]}
        assert client.put("/api/text-lists/blacklist", json=body).status_code == 403, "без Origin запис заборонено"
        saved = client.put("/api/text-lists/blacklist", json=body, headers={"Origin": ORIGIN})
        assert saved.status_code == 200 and saved.json()["revision"] == data["revision"] + 1
        assert client.put("/api/text-lists/blacklist", json=body, headers={"Origin": ORIGIN}).status_code == 409
        bad = {"entries": [{"raw_line": "фраза 68", "enabled": True}], "expected_revision": saved.json()["revision"]}
        result = client.put("/api/text-lists/blacklist", json=bad, headers={"Origin": ORIGIN})
        assert result.status_code == 422 and "числом" in result.json()["detail"]
        assert client.put("/api/text-lists/unknown", json=bad, headers={"Origin": ORIGIN}).status_code == 404


def test_new_database_is_seeded_from_the_single_defaults_file(tmp_path):
    """Усі початкові списки — з core/default_text_lists.txt; шаблони фраз — вимкнені."""
    from core import default_text_lists
    path = tmp_path / "events.db"
    init_dictionary_db(path)
    lists = read_lists(path)["lists"]
    for list_type in ("blacklist", "short_garbage", "aliases", "callsign_aliases", "phrase_templates"):
        assert [e["raw_line"] for e in lists[list_type]["entries"]] == list(default_text_lists.enabled(list_type))
    patterns = lists["phrase_patterns"]["entries"]
    assert patterns and not any(e["enabled"] for e in patterns)
    assert all(not e["raw_line"].startswith(("#", " ")) for e in patterns)
