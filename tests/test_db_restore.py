"""Відновлення з копії не повторює виданих ID подій — лише тимчасові бази."""

import os
import sqlite3

import pytest

from core.instance_lock import acquire_instance_lock
from services.database_lifecycle import RESTORE_SAFE_JUMP, restore_database
from services.db_backup import create_backup
from services.event_store import init_db, save_event
from services.telegram_offset_store import load_telegram_offset, save_telegram_offset


@pytest.fixture
def scene(tmp_path):
    db = tmp_path / "events.db"
    init_db(db)
    save_event("", "", "", "", "перша", db)
    save_telegram_offset(db, 100)
    backup = create_backup(db, tmp_path / "backups")
    # Після копії: нові події й посилання db:2, db:3 у Telegram.
    save_event("", "", "", "", "друга", db)
    save_event("", "", "", "", "третя", db)
    save_telegram_offset(db, 140)
    return db, backup, tmp_path


def texts(path):
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT text FROM radio_events ORDER BY id")]


def test_restored_database_never_reuses_event_ids(scene):
    db, backup, tmp = scene
    result = restore_database(db, backup, tmp / "backups", lock_path=tmp / ".lock")
    assert texts(db) == ["перша"]
    new_id = save_event("", "", "", "", "нова після відновлення", db)
    assert new_id == 4, "db:2 і db:3 зі старих повідомлень не вказують на нові події"
    assert result["next_event_id"] == 4 and result["ids_jumped"] is False
    assert load_telegram_offset(db) == 140, "старі команди Telegram не виконуються вдруге"


def test_previous_database_is_kept_and_stale_wal_removed(scene):
    db, backup, tmp = scene
    result = restore_database(db, backup, tmp / "backups", lock_path=tmp / ".lock")
    assert texts(result["previous_database"]) == ["перша", "друга", "третя"]
    assert not (tmp / "events.db-wal").exists() or os.path.getsize(tmp / "events.db-wal") == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_unreadable_live_database_forces_safe_id_jump(scene):
    db, backup, tmp = scene
    for suffix in ("-wal", "-shm"):
        (tmp / f"events.db{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"not a database")
    result = restore_database(db, backup, tmp / "backups", lock_path=tmp / ".lock")
    assert result["ids_jumped"] is True
    assert save_event("", "", "", "", "нова", db) >= RESTORE_SAFE_JUMP
    assert list((tmp / "backups").glob("events-broken-*.db"))


def test_restore_refused_while_receiver_runs(scene):
    db, backup, tmp = scene
    held = acquire_instance_lock(tmp / ".lock")
    try:
        with pytest.raises(RuntimeError, match="зупиніть приймач"):
            restore_database(db, backup, tmp / "backups", lock_path=tmp / ".lock")
    finally:
        os.close(held)
    assert texts(db) == ["перша", "друга", "третя"]


def test_damaged_backup_is_rejected_without_touching_live(scene):
    db, _, tmp = scene
    broken = tmp / "broken.db"
    broken.write_bytes(b"garbage" * 100)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        restore_database(db, broken, tmp / "backups", lock_path=tmp / ".lock")
    assert texts(db) == ["перша", "друга", "третя"]
    assert not list(tmp.glob(".events-restore-*"))
