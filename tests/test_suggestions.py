"""Пропозиції з розмітки: збір у всіх списках, нові позначаються, застосовані зникають."""
import sqlite3

from services.dictionary_store import init_db
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


def test_repeated_findings_become_suggestions_and_disappear_once_applied(tmp_path, monkeypatch):
    import services.suggestions as suggestions_module
    # Ефект алиаса на еталонах перевіряє окремий тест; тут — що кандидат доходить до пропозицій.
    monkeypatch.setattr(suggestions_module, "_alias_effects", lambda db, store, cs: [(c, "+1 знайдених позивних") for c in cs])
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
    assert groups["dictionary"]["lines"] == [
        {"list": "callsigns", "callsign": "клен", "number": "17"},
    ]
    assert refresh(path, store)[1] == [], "повторна перевірка нових не додає"
    data = read_lists(path)
    entries = [{"raw_line": e["raw_line"], "enabled": e["enabled"]} for e in data["lists"]["short_garbage"]["entries"]]
    write_list(path, "short_garbage", entries + [{"raw_line": "та па", "enabled": True}], data["revision"])
    refresh(path, store)
    assert "noise" not in {g["kind"] for g in overview(path)["groups"]}, "застосоване зникає"


def test_disabled_phrase_patterns_that_only_help_are_suggested(tmp_path, monkeypatch):
    # Перевіряється сам шаблон фраз: кінцеве правило «прийнято, прийом» виправило б приклад і без нього.
    monkeypatch.setattr('core.template_text.CLOSING_TURN', False)
    path = base_db(tmp_path)
    store = archive(tmp_path, [("клен 15, приняте, прийом", "Клен 15, прийнято, прийом", "speech")])
    data = read_lists(path)
    write_list(path, "phrase_patterns", [{"raw_line": "{П}, прийнято, прийом", "enabled": False}], data["revision"])
    refresh(path, store, deep=True)
    groups = {g["kind"]: g for g in overview(path)["groups"]}
    assert groups["pattern"]["count"] == 1 and "{П}, прийнято, прийом" in groups["pattern"]["examples"][0]
    assert groups["pattern"]["lines"] == [
        {"list": "phrase_patterns", "line": "{П}, прийнято, прийом"},
    ]


def test_text_countercheck_measures_each_active_phrase_pattern(tmp_path, monkeypatch):
    # Перевіряється сам шаблон фраз: кінцеве правило «прийнято, прийом» виправило б приклад і без нього.
    monkeypatch.setattr('core.template_text.CLOSING_TURN', False)
    from services.text_replay import countercheck
    path = base_db(tmp_path)
    store = archive(tmp_path, [("клен 15, приняте, прийом", "Клен 15, прийнято, прийом", "speech")])
    data = read_lists(path)
    write_list(path, "phrase_patterns",
                [{"raw_line": "{П}, прийнято, прийом", "enabled": True}], data["revision"])
    result = countercheck(store.root, path)
    pattern = next(item for item in result["checks"] if item["kind"] == "phrase_pattern")
    assert result["records"] == 1
    assert pattern["effect"] == "helpful"
    assert pattern["no_edits_delta"] == 1
    assert pattern["worse"] == 0
    assert pattern["label"] == "{П}, прийнято, прийом"


def test_text_countercheck_measures_active_blacklist_entries(tmp_path):
    from services.text_replay import countercheck
    path = base_db(tmp_path)
    store = archive(tmp_path, [
        ("дякую за перегляд", "Клен 15, прийом", "speech"),
        ("дякую за перегляд", "", "noise"),
    ])
    data = read_lists(path)
    write_list(path, "blacklist", [{"raw_line": "дякую за перегляд", "enabled": True}], data["revision"])
    result = countercheck(store.root, path)
    blacklist = next(item for item in result["checks"] if item["kind"] == "blacklist")
    assert blacklist["effect"] == "mixed"
    assert blacklist["missed_delta"] == 1
    assert blacklist["false_text_delta"] == -1
    assert blacklist["label"] == "дякую за перегляд"


def test_reference_recheck_finds_label_role_event_and_text_conflicts(tmp_path):
    import core.callsigns as callsigns
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
    assert not [i for i in dictionary_conflicts(path) if i["kind"] == "dict_overlap"], \
        "один номер у кількох позивних — звичайна справа, не нагадуємо"


def test_ordinal_role_is_understood_through_the_dictionary(tmp_path, monkeypatch):
    import core.callsigns as callsigns
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


def test_reference_text_check_ignores_codes_and_names_outside_the_dictionary(tmp_path):
    from services import daily_codes
    from services.suggestions import reference_checks
    path = base_db(tmp_path)
    daily_codes.write(path, daily_codes.local_day(), "48 62")
    store = RecordingStore(tmp_path / "recordings")
    for text in ("Клен 15, 48 63, прийом", "Злива 42, прийом", "Клен 99, прийом"):
        rid = store.save(pcm(segment()), 16000, {"status": "ready"})
        store.review(rid, text, "speech", 0)
    assert not [i for i in reference_checks(path, store) if i["kind"] == "ref_text"], \
        "неповна сітка кодів, позивні й номери поза словником — не помилки еталона"


def test_done_hides_current_suggestions_until_a_new_one_appears(tmp_path):
    from services.suggestions import dismiss, restore
    path = base_db(tmp_path)
    store = archive(tmp_path, [("та па", "", "room"), ("та па", "", "noise")])
    refresh(path, store)
    assert dismiss(path, "noise") == 1
    found = overview(path)
    assert "noise" not in {g["kind"] for g in found["groups"]} and found["hidden"] == 1
    refresh(path, store)
    assert "noise" not in {g["kind"] for g in overview(path)["groups"]}, "фонова перевірка не повертає прихованого"
    for _ in range(2):
        rid = store.save(pcm(segment()), 16000, {"status": "ready", "raw_text": "пау пау", "before_llm": "пау пау"})
        store.review(rid, "", "noise", 0)
    refresh(path, store)
    assert overview(path)["groups"][0]["examples"] == ["«пау пау» (×2)"], "нова пропозиція того ж виду видна"
    assert restore(path) == 1 and overview(path)["hidden"] == 0


def test_enabled_pattern_disappears_without_deep_check(tmp_path):
    path = base_db(tmp_path)
    store = archive(tmp_path, [("клен 15, приняте, прийом", "Клен 15, прийнято, прийом", "speech")])
    data = read_lists(path)
    write_list(path, "phrase_patterns", [{"raw_line": "{П}, прийнято, прийом", "enabled": False}], data["revision"])
    refresh(path, store, deep=True)
    data = read_lists(path)
    write_list(path, "phrase_patterns", [{"raw_line": "{П}, прийнято, прийом", "enabled": True}], data["revision"])
    refresh(path, store)
    assert "pattern" not in {g["kind"] for g in overview(path)["groups"]}


def test_worker_rechecks_soon_after_a_poke(tmp_path):
    import logging
    import time
    from services.suggestions import SuggestionWorker
    calls = []
    worker = SuggestionWorker(tmp_path / "x.db", None, logging.getLogger("t"), interval=3600, poke_delay=0.05)
    worker._refresh = lambda: calls.append(1)
    try:
        worker._stop.wait = lambda timeout=None, _w=worker._stop.wait: _w(min(timeout or 0, 0.05))
        worker.start()
        deadline = time.monotonic() + 2
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.poke()
        assert worker.pending
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 2
    finally:
        worker.stop()
        worker.thread.join(timeout=1)


def test_alias_is_suggested_only_when_it_changes_something_on_the_references(tmp_path, monkeypatch):
    import services.text_replay as replay
    import services.suggestions as suggestions_module
    path = base_db(tmp_path)
    store = archive(tmp_path, [("кльон 15, прийом", "Клен 15, прийом", "speech")])
    outcomes = iter([
        {"worse": 0, "better": 0, "before": {"callsigns_hit": 5, "callsigns_false": 1}, "after": {"callsigns_hit": 5, "callsigns_false": 1}},
        {"worse": 0, "better": 1, "before": {"callsigns_hit": 5, "callsigns_false": 1}, "after": {"callsigns_hit": 7, "callsigns_false": 1}},
        {"worse": 0, "better": 0, "before": {"callsigns_hit": 5, "callsigns_false": 1}, "after": {"callsigns_hit": 6, "callsigns_false": 2}},
    ])
    monkeypatch.setattr(replay, "compare", lambda *a, **k: next(outcomes))
    candidates = [{"heard": w, "callsign": "клен"} for w in ("нічого", "корисний", "хибний")]
    result = suggestions_module._alias_effects(path, store, candidates)
    assert [(c["heard"], effect) for c, effect in result] == [("корисний", "+2 знайдених позивних, краще фраз: 1")]
