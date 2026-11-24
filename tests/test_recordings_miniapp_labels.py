"""Правки з Mini App автоматично стають еталонами записів архіву."""

import sqlite3

from shared.dictionary_store import init_db as init_dictionary_db
from shared.event_store import init_db as init_event_db, save_event
from shared.miniapp_correction_editor import save_edit_draft
from shared.recordings import RecordingStore, label_archive_from_correction

PCM = b"\0\0" * 1600


def archive(tmp_path):
    return RecordingStore(tmp_path / ".local-recordings")


def saved(store, fix_id, raw="Вишня, 15, кісті"):
    return store.save(PCM, 16000, {"status": "ready", "raw_text": raw, "fix_id": fix_id})


def test_correction_labels_linked_record(tmp_path):
    store = archive(tmp_path)
    record = saved(store, "db:5")
    other = saved(store, "db:6")

    assert store.label_from_correction("db:5", "Вишня 15, прийом") == [record]

    row = store.get(record)
    assert (row["label"], row["reference"], row["reference_source"]) == ("speech", "Вишня 15, прийом", "miniapp")
    assert store.get(other)["reference"] is None


def test_newer_correction_replaces_older_but_manual_label_wins(tmp_path):
    store = archive(tmp_path)
    record = saved(store, "mem:" + "a" * 32)
    store.label_from_correction("mem:" + "a" * 32, "Вишня 15")
    store.label_from_correction("mem:" + "a" * 32, "Вишня 15, прийом")
    assert store.get(record)["reference"] == "Вишня 15, прийом"

    revision = store.get(record)["revision"]
    store.review(record, "Вишня 16, прийом", "speech", revision)
    assert store.get(record)["reference_source"] is None
    assert store.label_from_correction("mem:" + "a" * 32, "Клен 1") == []
    assert store.get(record)["reference"] == "Вишня 16, прийом"


def test_operator_noise_label_is_not_overwritten(tmp_path):
    store = archive(tmp_path)
    record = saved(store, "db:7", raw="Дуб")
    store.review(record, "", "room", 0)
    assert store.label_from_correction("db:7", "Дуб 15") == []
    assert store.get(record)["label"] == "room"


def test_case_only_or_unchanged_edit_is_not_a_reference(tmp_path):
    store = archive(tmp_path)
    record = saved(store, "db:8", raw="Дуб")
    db_path = tmp_path / "events.db"
    assert label_archive_from_correction(db_path, "db:8", "Дуб", "дуб.") == []
    assert label_archive_from_correction(db_path, "db:8", "Дуб", "Дуб") == []
    assert store.get(record)["label"] is None


def test_missing_archive_is_not_created(tmp_path):
    assert label_archive_from_correction(tmp_path / "events.db", "db:1", "а", "б") == []
    assert not (tmp_path / ".local-recordings").exists()


def test_mini_app_edit_reaches_archive(tmp_path):
    db_path = str(tmp_path / "events.db")
    init_dictionary_db(db_path)
    init_event_db(db_path)
    event_id = save_event("", "", "", "", "Вишня, 15, кісті", db_path=db_path, raw_text="Вишня, 15, кісті")
    store = archive(tmp_path)
    record = saved(store, f"db:{event_id}")

    save_edit_draft(db_path, f"db:{event_id}", "Вишня, 15, кісті", "Вишня 15, прийом")

    row = store.get(record)
    assert (row["label"], row["reference"]) == ("speech", "Вишня 15, прийом")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT text FROM radio_events WHERE id=?", (event_id,)).fetchone()[0] == "Вишня 15, прийом"
