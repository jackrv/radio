"""Відтворюваний багатопроцесний стрес і відновлення після kill; лише tmp_path."""

import multiprocessing as mp
import os
import select
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shared.sqlite_connection import get_db
from tools.check_sqlite_concurrency import run_stress


@pytest.mark.parametrize("kind", ["learning", "lab"])
@pytest.mark.parametrize("fail_backup", [False, True])
def test_snapshot_connections_close_before_file_reads_and_on_failure(tmp_path, monkeypatch, kind, fail_backup):
    import config
    from shared.archive_learning import learn_saved_archive
    from shared.recordings import RecordingStore
    from shared.user_store import bootstrap_admin, init_users_db
    from lab.asr_lab import snapshot_database

    path = tmp_path / "events.db"
    init_users_db(path)
    bootstrap_admin(path, 1001)
    store = RecordingStore(tmp_path / "recordings")
    store.save(b"\0\0" * 1600, 16000, {"status": "ready", "raw_text": "синтетичний текст"})
    original, connections = sqlite3.connect, []

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.path, self.closed = str(args[0]), False
            connections.append(self)

        def close(self):
            self.closed = True
            super().close()

        def backup(self, target, **kwargs):
            super().backup(target, **kwargs)
            if fail_backup:
                raise sqlite3.OperationalError("synthetic backup failure")

    def connect(*args, **kwargs):
        return original(*args, **dict(kwargs, factory=TrackedConnection))

    read_bytes = Path.read_bytes

    def checked_read_bytes(file):
        assert all(connection.closed for connection in connections if connection.path == str(file)), \
            "не можна читати файл копії напряму до закриття її SQLite-з'єднання"
        return read_bytes(file)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(Path, "read_bytes", checked_read_bytes)
    monkeypatch.setattr(config, "DB_PATH", str(path))

    def run():
        if kind == "learning":
            return learn_saved_archive(path, store, 1001)
        return snapshot_database(tmp_path / "snapshot.db")

    if fail_backup:
        with pytest.raises(sqlite3.OperationalError, match="synthetic backup failure"):
            run()
    else:
        run()
    assert connections and all(connection.closed for connection in connections)


def test_stress_keeps_committed_counts_and_closes_all_children():
    before = {process.pid for process in mp.active_children()}
    result = run_stress(iterations=8, interval=.005)
    assert result["integrity"] == "ok"
    assert result["committed_counts"] == [8, 8]
    assert result["recordings"] == 9
    assert result["exitcodes"] == [0, 0, 0, 0]
    assert result["roles"]["receiver"]["background_ops"] > 0
    assert result["roles"]["review"]["background_ops"] > 0
    assert {process.pid for process in mp.active_children()} == before


def test_stress_deadline_cleans_up_its_children():
    before = {process.pid for process in mp.active_children()}
    with pytest.raises(TimeoutError):
        run_stress(iterations=1_000_000, timeout=.01)
    assert {process.pid for process in mp.active_children()} == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX process kill")
def test_killed_writer_rolls_back_and_next_writer_can_commit(tmp_path):
    path = tmp_path / "events.db"
    with get_db(path) as db:
        db.execute("CREATE TABLE synthetic(value INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO synthetic VALUES(1)")
    source = """
import sys
from shared.sqlite_connection import get_db
with get_db(sys.argv[1]) as db:
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT INTO synthetic VALUES(2)')
    print('transaction-open', flush=True)
    sys.stdin.read()
"""
    child = subprocess.Popen([sys.executable, "-c", source, str(path)],
                             cwd=Path(__file__).resolve().parents[1], text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([child.stdout], [], [], 10)[0], "дочірній writer не готовий"
        assert child.stdout.readline().strip() == "transaction-open"
        with get_db(path) as db:
            assert db.execute("SELECT value FROM synthetic").fetchall() == [(1,)]
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode < 0
        with get_db(path) as db:
            db.execute("INSERT INTO synthetic VALUES(3)")
        with get_db(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("SELECT value FROM synthetic ORDER BY value").fetchall() == [(1,), (3,)]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
