"""Автоматичні резервні копії — лише тимчасові бази."""

from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import Mock

import pytest

from services.db_backup import (
    auto_backups, backup_if_due, create_backup, latest_backup_age, prune_backups,
)
from services.event_store import init_db, save_event

T0 = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    save_event("", "", "", "", "Липка 700", path)
    return path


def test_backup_is_verified_private_and_complete(live, tmp_path):
    copy = create_backup(live, tmp_path / "backups", now=T0)
    assert copy.name == "events-auto-20261115T030000Z.db"
    assert copy.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(copy) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT text FROM radio_events").fetchall() == [("Липка 700",)]
    assert not list((tmp_path / "backups").glob(".*.tmp"))


def test_only_last_auto_backups_are_kept_and_others_untouched(live, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    manual = folder / "events-before-reset-20261114T131009441651Z.db"
    manual.write_bytes(b"manual")
    for day in range(9):
        create_backup(live, folder, keep=7, now=T0 + timedelta(days=day))
    names = [p.name for p in auto_backups(folder)]
    assert len(names) == 7
    assert names[0] == "events-auto-20261123T030000Z.db"
    assert "events-auto-20261115T030000Z.db" not in names
    assert manual.read_bytes() == b"manual", "ручні копії не видаляються"


def test_backup_runs_once_per_day(live, tmp_path):
    folder = tmp_path / "backups"
    assert backup_if_due(live, folder, now=T0) is not None
    assert backup_if_due(live, folder, now=T0 + timedelta(hours=23)) is None
    assert backup_if_due(live, folder, now=T0 + timedelta(hours=24, minutes=1)) is not None
    assert len(auto_backups(folder)) == 2
    assert latest_backup_age(folder, now=T0 + timedelta(hours=25)) == pytest.approx(3540)


def test_failure_is_logged_and_never_raises(tmp_path):
    log = Mock()
    assert backup_if_due(tmp_path / "missing.db", tmp_path / "backups", log=log, now=T0) is None
    log.exception.assert_called_once()


def test_corrupt_copy_is_discarded(live, tmp_path, monkeypatch):
    import services.db_backup as module

    real_connect = sqlite3.connect

    class Damaged(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA integrity_check"):
                return Mock(fetchone=Mock(return_value=("damaged",)))
            return super().execute(sql, *args)

    def connect(target, *args, **kwargs):
        if str(target).endswith(".tmp"):
            kwargs["factory"] = Damaged
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(RuntimeError):
        create_backup(live, tmp_path / "backups", now=T0)
    assert list((tmp_path / "backups").iterdir()) == []


def test_abandoned_temp_file_is_cleaned(live, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / ".events-auto-20261101T000000Z.db.tmp").write_bytes(b"partial")
    prune_backups(folder)
    assert list(folder.iterdir()) == []
