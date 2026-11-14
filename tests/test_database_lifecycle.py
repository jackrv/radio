from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from database import import_dictionaries
from services.database_lifecycle import initialize_database, reset_database, export_dictionaries
from services.event_store import save_event
from services.sqlite_connection import get_db
from services.user_store import bootstrap_admin
from services.telegram_offset_store import save_telegram_offset, load_telegram_offset


def sources(folder):
    folder.mkdir(exist_ok=True)
    for name, text in {"callsigns.txt": "Ліра: 68\n# @radio345-disabled: Ліпка: 700\n", "commands.txt": "Прийом\n", "telegram.txt": "Ліра: 68\n"}.items():
        (folder / name).write_text(text)


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source"
    sources(source)
    db = tmp_path / "events.db"
    initialize_database(db, source)
    bootstrap_admin(db, 1001)
    return db, source


def test_reset_exports_backs_up_and_keeps_admin_and_identity_counters(setup, tmp_path):
    db, _ = setup
    old_id = save_event("", "", "", "", "тест", db)
    save_telegram_offset(db, 700)
    export = tmp_path / "export"
    result = reset_database(db, export, tmp_path / "backup")
    assert result["active_admins"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM radio_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 0
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM radio_events").fetchone()[0] == 1
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert load_telegram_offset(db) == 700
    initialize_database(db, export)
    assert save_event("", "", "", "", "новий тест", db) > old_id
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT raw_line,enabled FROM dictionary_entries WHERE dictionary_type='callsigns' ORDER BY line_number").fetchall() == [("Ліра: 68", 1), ("Ліпка: 700", 0)]


def test_import_deletes_only_working_sources_after_commit(tmp_path):
    source = tmp_path / "source"
    sources(source)
    template = source / "callsigns.example.txt"
    template.write_text("# Порожній шаблон\n")
    db = tmp_path / "events.db"
    result = initialize_database(db, source, delete_sources=True)
    assert result["cleanup_remaining"] == []
    assert not (source / "callsigns.txt").exists()
    assert template.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 4


def test_failed_import_never_deletes_sources(setup):
    db, source = setup
    with pytest.raises(RuntimeError):
        import_dictionaries(source, db, delete_sources=True)
    assert all((source / name).exists() for name in ("callsigns.txt", "commands.txt", "telegram.txt"))


def test_import_failure_rolls_back_all_categories_and_preserves_sources(tmp_path):
    source = tmp_path / "source"
    sources(source)
    db = tmp_path / "events.db"
    from database import init_db
    init_db(db)
    with get_db(db) as conn:
        conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON dictionary_entries WHEN NEW.dictionary_type='telegram' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.Error):
        import_dictionaries(source, db, delete_sources=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 0
    assert (source / "callsigns.txt").is_file()


def test_empty_templates_cannot_initialize_receiver(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("callsigns", "commands", "telegram"):
        (source / f"{name}.txt").write_text("# Порожній шаблон\n")
    with pytest.raises(ValueError):
        initialize_database(tmp_path / "events.db", source, delete_sources=True)
    assert (source / "callsigns.txt").exists()


def test_export_never_overwrites_operator_files(setup):
    db, source = setup
    original = (source / "callsigns.txt").read_bytes()
    with pytest.raises(FileExistsError):
        export_dictionaries(db, source)
    assert (source / "callsigns.txt").read_bytes() == original


def test_active_receiver_blocks_reset(setup, tmp_path):
    db, _ = setup
    from services.receiver_status import init_receiver_db, record_heartbeat
    init_receiver_db(db)
    record_heartbeat(db, "test", state="listening")
    with pytest.raises(RuntimeError):
        reset_database(db, tmp_path / "export", tmp_path / "backup")
    assert not (tmp_path / "export").exists()


def test_failed_reset_preserves_database_and_backup(setup, tmp_path):
    db, _ = setup
    save_event("", "", "", "", "тест", db)
    with get_db(db) as conn:
        conn.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON radio_events BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.Error):
        reset_database(db, tmp_path / "export", tmp_path / "backup")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM radio_events").fetchone()[0] == 1
    assert len(list((tmp_path / "backup").glob("*.db"))) == 1


def test_concurrent_events_keep_unique_monotonic_ids(setup):
    db, _ = setup
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: save_event("", "", "", "", "тест", db), range(12)))
    assert len(set(ids)) == 12
    assert sorted(ids) == list(range(1, 13))
