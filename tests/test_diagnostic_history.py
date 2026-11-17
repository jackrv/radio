"""Історія сесій: тимчасові каталоги й власні тестові процеси, без SQLite/моделей."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import uuid

import pytest

from core.diagnostics import RuntimeDiagnostics
from core.diagnostic_history import (
    DiagnosticsHistory, atomic_write, history_directory, snapshot_filename,
)


def snapshot(*, session=None, started=None, updated=None):
    stamp = time.time() if started is None else started
    return {"schema": 1, "session": session or uuid.uuid4().hex,
            "started_at": stamp, "updated_at": stamp if updated is None else updated,
            "session_status": "running", "counters": {"asr.measured_segments": 5},
            "gauges": {}, "timings": {}}


def test_restart_retains_previous_session_and_current_snapshot(tmp_path):
    path = tmp_path / "diagnostics.json"
    first = RuntimeDiagnostics()
    first.configure(path)
    first.count("asr.measured_segments", 23)
    first.observe("asr.whisper", 2)
    first.finish()
    final = json.loads(path.read_text())
    second = RuntimeDiagnostics()
    second.configure(path)
    store = DiagnosticsHistory(history_directory(path))
    assert len(store.list_sessions()["items"]) == 2
    assert store.load(first.session) == final
    assert final["session_status"] == "finished" and final["ended_at"] >= final["started_at"]
    assert store.load(second.session)["counters"] == {}
    assert json.loads(path.read_text())["session"] == second.session
    assert (history_directory(path).stat().st_mode & 0o777) == 0o700
    assert all((file.stat().st_mode & 0o777) == 0o600 for file in history_directory(path).glob("*.json"))


def test_periodic_writes_update_one_file_and_skip_within_30_seconds(tmp_path, monkeypatch):
    path = tmp_path / "diagnostics.json"
    with patch("core.diagnostics.time.monotonic", return_value=100):
        metrics = RuntimeDiagnostics()
        metrics.configure(path)
        metrics.count("asr.segments", 4)
        metrics.flush()
        assert "asr.segments" not in DiagnosticsHistory(history_directory(path)).load(metrics.session)["counters"]
    with patch("core.diagnostics.time.monotonic", return_value=131):
        metrics.flush()
    store = DiagnosticsHistory(history_directory(path))
    assert len(store.list_sessions()["items"]) == 1
    assert store.load(metrics.session)["counters"]["asr.segments"] == 4


def test_first_upgrade_preserves_legacy_latest_before_overwriting(tmp_path):
    path = tmp_path / "diagnostics.json"
    previous = snapshot()
    previous.pop("session_status")
    path.write_text(json.dumps(previous))
    current = RuntimeDiagnostics()
    current.configure(path)
    store = DiagnosticsHistory(history_directory(path))
    assert store.load(previous["session"]) == previous
    assert json.loads(path.read_text())["session"] == current.session
    assert next(row for row in store.list_sessions()["items"] if row["session"] == previous["session"])["session_status"] == "unknown"


def test_older_latest_cannot_downgrade_a_newer_archive(tmp_path):
    old = snapshot(updated=time.time() - 10)
    newer = {**old, "updated_at": time.time(), "session_status": "finished", "ended_at": time.time()}
    path = tmp_path / "diagnostics.json"
    store = DiagnosticsHistory(history_directory(path))
    store.save(newer)
    path.write_text(json.dumps(old))
    RuntimeDiagnostics().configure(path)
    assert store.load(old["session"]) == newer


def test_unavailable_archive_does_not_destroy_the_only_previous_snapshot(tmp_path):
    path = tmp_path / "diagnostics.json"
    previous = snapshot()
    path.write_text(json.dumps(previous))
    blocked = tmp_path / ".local-diagnostics"
    blocked.write_text("not a directory")
    metrics = RuntimeDiagnostics()
    metrics.configure(path)
    metrics.count("still_running")
    metrics.flush(force=True)
    assert json.loads(path.read_text()) == previous
    blocked.unlink()
    metrics.flush(force=True)
    store = DiagnosticsHistory(blocked)
    assert store.load(previous["session"]) == previous
    assert json.loads(path.read_text())["counters"]["still_running"] == 1


def test_new_session_keeps_latest_available_when_history_write_fails(tmp_path):
    path = tmp_path / "diagnostics.json"
    (tmp_path / ".local-diagnostics").write_text("not a directory")
    metrics = RuntimeDiagnostics()
    metrics.configure(path)
    metrics.count("still_running")
    metrics.finish()
    assert json.loads(path.read_text())["counters"]["still_running"] == 1


def test_archive_survives_an_unavailable_latest_path(tmp_path):
    path = tmp_path / "missing" / "diagnostics.json"
    metrics = RuntimeDiagnostics()
    metrics.configure(path, history_dir=tmp_path / "history")
    metrics.count("still_running")
    metrics.finish()
    assert DiagnosticsHistory(tmp_path / "history").load(metrics.session)["counters"]["still_running"] == 1
    assert not path.exists()


def test_corrupt_latest_is_replaced_without_importing_private_fields(tmp_path):
    path = tmp_path / "diagnostics.json"
    path.write_text('{"raw_text":"PRIVATE",')
    metrics = RuntimeDiagnostics()
    metrics.configure(path)
    assert "PRIVATE" not in path.read_text()
    assert len(DiagnosticsHistory(history_directory(path)).list_sessions()["items"]) == 1


def test_atomic_write_failure_leaves_valid_old_snapshot_and_cleans_temporary(tmp_path):
    target = tmp_path / "snapshot.json"
    atomic_write(target, {"counter": 1})
    with patch("core.diagnostic_history.os.replace", side_effect=OSError("synthetic failure")):
        with pytest.raises(OSError):
            atomic_write(target, {"counter": 2})
    assert json.loads(target.read_text()) == {"counter": 1}
    assert not list(tmp_path.glob(".radio-diagnostics-*"))


def test_finish_waits_for_inflight_flush_and_writes_finished_marker(tmp_path):
    path = tmp_path / "diagnostics.json"
    metrics = RuntimeDiagnostics()
    metrics.configure(path)
    entered = threading.Event()
    release = threading.Event()
    original = metrics.history.save

    def slow_save(data):
        entered.set()
        assert release.wait(3)
        return original(data)

    metrics.history.save = slow_save
    with ThreadPoolExecutor(2) as pool:
        flushing = pool.submit(metrics.flush, True)
        assert entered.wait(3)
        closing = pool.submit(metrics.finish)
        deadline = time.monotonic() + 3
        while metrics.snapshot()["session_status"] != "finished" and time.monotonic() < deadline:
            time.sleep(0.001)
        assert metrics.snapshot()["session_status"] == "finished"
        assert not closing.done()
        release.set()
        flushing.result(timeout=3)
        closing.result(timeout=3)
    store = DiagnosticsHistory(history_directory(path))
    assert store.load(metrics.session)["session_status"] == "finished"
    assert json.loads(path.read_text())["session_status"] == "finished"


def test_sigkill_leaves_last_periodic_snapshot_and_allows_a_new_session(tmp_path):
    path = tmp_path / "diagnostics.json"
    code = '''
import sys,time
from core.diagnostics import RuntimeDiagnostics
m=RuntimeDiagnostics()
m.configure(sys.argv[1])
m.count("asr.measured_segments",17)
m.flush(force=True)
print(m.session,flush=True)
time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, "-u", "-c", code, str(path)],
                               cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
        session = process.stdout.readline().strip()
        assert len(session) == 32
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
    second = RuntimeDiagnostics()
    second.configure(path)
    saved = DiagnosticsHistory(history_directory(path)).load(session)
    assert saved["counters"]["asr.measured_segments"] == 17
    assert saved["session_status"] == "running" and "ended_at" not in saved


def test_retention_removes_oldest_sessions_but_not_other_files(tmp_path):
    store = DiagnosticsHistory(tmp_path / "history", max_sessions=2)
    first = store.save(snapshot(started=time.time() - 100))
    os.utime(first, (time.time()-100, time.time()-100))
    second = store.save(snapshot(started=time.time() - 50))
    os.utime(second, (time.time()-50, time.time()-50))
    unrelated = store.directory / "keep.json"
    unrelated.write_text("PRIVATE")
    third = store.save(snapshot())
    assert not first.exists() and second.exists() and third.exists()
    assert unrelated.read_text() == "PRIVATE"


def test_retention_age_bytes_and_incomplete_files(tmp_path):
    store = DiagnosticsHistory(tmp_path / "history", max_bytes=1500)
    old = store.save(snapshot())
    os.utime(old, (time.time()-15*86400, time.time()-15*86400))
    fresh = store.save(snapshot())
    assert not old.exists() and fresh.exists()
    for n in range(10):
        data = snapshot(started=time.time()+n)
        data["gauges"] = {"audio.segments_queue": 0}
        store.save(data)
    managed = list(store.directory.glob("*.json"))
    assert sum(file.stat().st_size for file in managed) <= 1500
    stale_temp = store.directory / ".radio-diagnostics-old"
    fresh_temp = store.directory / ".radio-diagnostics-active"
    stale_temp.write_text("incomplete"); fresh_temp.write_text("incomplete")
    os.utime(stale_temp, (time.time()-3601,time.time()-3601))
    store.prune()
    assert not stale_temp.exists() and fresh_temp.exists()


def test_symlinks_traversal_and_invalid_archives_are_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "history"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        DiagnosticsHistory(linked).save(snapshot())
    assert not list(outside.iterdir())
    store = DiagnosticsHistory(tmp_path / "real")
    data = snapshot()
    store.save(data)
    secret = outside / "secret"
    secret.write_text("PRIVATE")
    valid_name = snapshot_filename(snapshot())
    (store.directory / valid_name).symlink_to(secret)
    (store.directory / snapshot_filename(snapshot())).write_text("broken JSON")
    assert len(store.list_sessions()["items"]) == 1
    with pytest.raises(ValueError):
        store.load("../outside/secret")
    assert secret.read_text() == "PRIVATE"


def test_legacy_import_does_not_archive_unrelated_private_fields(tmp_path):
    previous = snapshot()
    previous["raw_text"] = "PRIVATE_PHRASE"
    previous["audio"] = "PRIVATE_AUDIO"
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps(previous))
    RuntimeDiagnostics().configure(path)
    store = DiagnosticsHistory(history_directory(path))
    saved = json.dumps(store.load(previous["session"]))
    assert "PRIVATE" not in saved and '"raw_text"' not in saved and '"audio"' not in saved


def test_periodic_diagnostics_still_flush_when_heartbeat_database_write_fails(tmp_path):
    import sqlite3
    from services import receiver_status

    flushed = threading.Event()
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append(None)
        if len(calls) > 1:
            raise sqlite3.OperationalError("synthetic busy database")

    with patch.object(receiver_status, "record_heartbeat", side_effect=heartbeat), \
            patch.object(receiver_status.runtime, "flush", side_effect=flushed.set):
        with receiver_status.receiver_heartbeat(tmp_path / "heartbeat.db", interval=0.01):
            assert flushed.wait(2)
