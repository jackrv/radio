"""Пропозиції з розмітки: збір у всіх списках, нові позначаються, застосовані зникають."""
import sqlite3

from database import init_db
from services.recordings import RecordingStore
from services.suggestions import collect, overview, refresh
from services.text_lists import read_lists, write_list
from tests.audio_helpers import pcm, segment


def archive(tmp_path, items):
    store = RecordingStore(tmp_path / "recordings")
    for raw, reference, label in items:
        rid = store.save(pcm(segment()), 16000, {"status": "ready", "raw_text": raw, "before_llm": raw})
        store.review(rid, reference, label, 0)
    return store


def base_db(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 1, 'Клен: 15 16')")
    return path


def test_repeated_findings_become_suggestions_and_disappear_once_applied(tmp_path):
    path = base_db(tmp_path)
    store = archive(tmp_path, [
        ("кльон 15, прийом", "Клен 15, прийом", "speech"), ("кльон 16, прийом", "Клен 16, прийом", "speech"),
        ("та па", "", "room"), ("та па", "", "noise"),
        ("клен 17, прийом", "Клен 17, прийом", "speech"), ("клен 17", "Клен 17", "speech"),
    ])
    kinds = {s["kind"] for s in collect(path, store)}
    assert {"alias", "noise", "dictionary"} <= kinds
    current, fresh = refresh(path, store, deep=False)
    assert fresh and len(fresh) == len(current)
    groups = {g["kind"]: g for g in overview(path)["groups"]}
    assert groups["noise"]["new"] == 1 and groups["noise"]["target"] == "texts:short_garbage"
    assert refresh(path, store)[1] == [], "повторна перевірка нових не додає"
    data = read_lists(path)
    entries = [{"raw_line": e["raw_line"], "enabled": e["enabled"]} for e in data["lists"]["short_garbage"]["entries"]]
    write_list(path, "short_garbage", entries + [{"raw_line": "та па", "enabled": True}], data["revision"])
    refresh(path, store)
    assert "noise" not in {g["kind"] for g in overview(path)["groups"]}, "застосоване зникає"


def test_disabled_phrase_patterns_that_only_help_are_suggested(tmp_path):
    path = base_db(tmp_path)
    store = archive(tmp_path, [("клен 15, приняте, прийом", "Клен 15, прийнято, прийом", "speech")])
    data = read_lists(path)
    write_list(path, "phrase_patterns", [{"raw_line": "{П}, прийнято, прийом", "enabled": False}], data["revision"])
    refresh(path, store, deep=True)
    groups = {g["kind"]: g for g in overview(path)["groups"]}
    assert groups["pattern"]["count"] == 1 and "{П}, прийнято, прийом" in groups["pattern"]["examples"][0]


def test_reference_recheck_finds_label_role_event_and_text_conflicts(tmp_path):
    import callsigns
    from services.event_store import init_db as init_events, save_event
    from services.suggestions import dictionary_conflicts, reference_checks
    path = base_db(tmp_path)
    init_events(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 2, 'Дуб: 15 4')")
    event = save_event("клен 15", "", "", "", "кльон 15 прийом", path)
    store = RecordingStore(tmp_path / "recordings")
    def add(data, reference, label, **roles):
        rid = store.save(pcm(segment()), 16000, {"status": "ready", **data})
        store.review(rid, reference, label, 0, **roles)
        return rid
    empty = add({}, "", "speech")
    noisy = add({}, "клен 15", "room")
    roles = add({}, "Клен 15, прийнято, прийом", "speech", reference_recipient="дуб 4", reference_sender="клен 15")
    stale = add({"fix_id": f"db:{event}"}, "Клен 15, прийом", "speech")
    typo = add({}, "Кленн 16, прийом", "speech")
    found = {(i["kind"], i["record"]) for i in reference_checks(path, store)}
    assert ("ref_label", empty) in found and ("ref_label", noisy) in found
    assert ("ref_roles", roles) in found, "«кому: дуб 4» у тексті немає"
    assert ("ref_event", stale) in found and ("ref_text", typo) in found
    overlap = [i["title"] for i in dictionary_conflicts(path) if i["kind"] == "dict_overlap"]
    assert overlap == ["номер 15 є в позивних дуб, клен"]


def test_ordinal_role_is_understood_through_the_dictionary(tmp_path, monkeypatch):
    import callsigns
    from services.suggestions import reference_checks
    path = base_db(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES ('callsigns', 2, 'Дуб: 1 2 3 4')")
    from services.text_lists import read_lists, write_list
    data = read_lists(path)
    write_list(path, "callsign_aliases", [{"raw_line": "дуб: четвертий", "enabled": True}], data["revision"])
    store = RecordingStore(tmp_path / "recordings")
    rid = store.save(pcm(segment()), 16000, {"status": "ready"})
    store.review(rid, "Клен 15, я четвертий, прийнято, прийом", "speech", 0, reference_recipient="клен 15", reference_sender="дуб 4")
    assert not [i for i in reference_checks(path, store) if i["kind"] == "ref_roles"]
