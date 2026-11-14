from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from database import import_dictionaries
from services.database_lifecycle import initialize_database, reset_database, export_dictionaries
from services.event_store import save_event
from services.sqlite_connection import get_db
from services.user_store import bootstrap_admin
from services.telegram_offset_store import save_telegram_offset, load_telegram_offset


SOURCE = """# коментар не імпортується
[callsigns]
Ліра: 68
# @radio345-disabled: Ліпка: 700

[commands]
Прийом

[telegram]
Ліра: 68
"""


def sources(folder):
    folder.mkdir(exist_ok=True)
    path = folder / "dictionaries.txt"
    path.write_text(SOURCE, encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    source = sources(tmp_path / "source")
    db = tmp_path / "events.db"
    initialize_database(db, source)
    bootstrap_admin(db, 1001)
    return db, source


def test_reset_exports_backs_up_and_keeps_admin_and_identity_counters(setup, tmp_path):
    db, _ = setup
    old_id = save_event("", "", "", "", "тест", db)
    save_telegram_offset(db, 700)
    export = tmp_path / "export" / "dictionaries.txt"
    result = reset_database(db, export, tmp_path / "backup")
    assert result["exported"] == {"callsigns": 2, "commands": 1, "telegram": 1}
    assert export.stat().st_mode & 0o777 == 0o600
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


def test_import_deletes_only_working_source_after_commit(tmp_path):
    source = sources(tmp_path / "source")
    template = source.parent / "dictionaries.example.txt"
    template.write_text("[callsigns]\n# липка: 700\n")
    db = tmp_path / "events.db"
    result = initialize_database(db, source, delete_source=True)
    assert result["cleanup_remaining"] == []
    assert result["imported"] == {"callsigns": 2, "commands": 1, "telegram": 1}
    assert not source.exists()
    assert template.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 4


def test_failed_import_never_deletes_sources(setup):
    db, source = setup
    with pytest.raises(RuntimeError):
        import_dictionaries(source, db, delete_source=True)
    assert source.exists()


def test_import_failure_rolls_back_all_categories_and_preserves_sources(tmp_path):
    source = sources(tmp_path / "source")
    db = tmp_path / "events.db"
    from database import init_db
    init_db(db)
    with get_db(db) as conn:
        conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON dictionary_entries WHEN NEW.dictionary_type='telegram' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.Error):
        import_dictionaries(source, db, delete_source=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 0
    assert source.is_file()


def test_shipped_template_cannot_initialize_receiver(tmp_path):
    template = Path(__file__).resolve().parent.parent / "dictionaries.example.txt"
    source = tmp_path / "dictionaries.txt"
    source.write_bytes(template.read_bytes())
    with pytest.raises(ValueError, match="callsigns"):
        initialize_database(tmp_path / "events.db", source, delete_source=True)
    assert source.exists()
    assert not (tmp_path / "events.db").exists()


def test_export_never_overwrites_operator_files(setup):
    db, source = setup
    original = source.read_bytes()
    with pytest.raises(FileExistsError):
        export_dictionaries(db, source)
    assert source.read_bytes() == original


def test_active_receiver_blocks_reset(setup, tmp_path):
    db, _ = setup
    from services.receiver_status import init_receiver_db, record_heartbeat
    init_receiver_db(db)
    record_heartbeat(db, "test", state="listening")
    with pytest.raises(RuntimeError):
        reset_database(db, tmp_path / "export.txt", tmp_path / "backup")
    assert not (tmp_path / "export.txt").exists()


def test_failed_reset_preserves_database_and_backup(setup, tmp_path):
    db, _ = setup
    save_event("", "", "", "", "тест", db)
    with get_db(db) as conn:
        conn.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON radio_events BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.Error):
        reset_database(db, tmp_path / "export.txt", tmp_path / "backup")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM radio_events").fetchone()[0] == 1
    assert len(list((tmp_path / "backup").glob("*.db"))) == 1


def test_concurrent_events_keep_unique_monotonic_ids(setup):
    db, _ = setup
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: save_event("", "", "", "", "тест", db), range(12)))
    assert len(set(ids)) == 12
    assert sorted(ids) == list(range(1, 13))
