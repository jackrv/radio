"""Бекап Pi → Mac: перевірений, атомарний, не чіпає живу базу й чужі файли."""
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from shared import pull_backup as b
from shared.paths import place

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def pi(tmp_path):
    root = tmp_path / "hub"
    (root / ".local-recordings").mkdir(parents=True)
    for name, rows in (("events.db", 3), (".local-recordings/index.sqlite3", 5)):
        db = sqlite3.connect(root / name)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(x)")
        db.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(rows)])
        db.commit()
        db.close()
    return root


def command(root):
    return [sys.executable, "-I", "-", str(root)]


def count(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        db.close()


def test_backup_is_complete_private_and_leaves_no_traces(pi, tmp_path):
    live = sqlite3.connect(pi / "events.db")
    live.execute("INSERT INTO t VALUES(99)")
    live.commit()   # відкрите з'єднання, дані ще у WAL
    try:
        path = b.pull(command(pi), tmp_path / "out", now=START)
    finally:
        live.close()
    assert count(path / "events.db") == 4 and count(path / "index.sqlite3") == 5
    assert stat.S_IMODE((path / "events.db").stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(pi.glob("r345-backup-*")) and not list(path.parent.glob(".partial-*"))


def test_missing_database_fails_without_partial_result(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises((RuntimeError, tarfile.TarError, ValueError)):
        b.pull(command(tmp_path / "empty"), tmp_path / "out", now=START)
    assert [p.name for p in (tmp_path / "out").iterdir()] == [".lock"]


def test_unreachable_pi_fails_without_partial_result(tmp_path):
    with pytest.raises((RuntimeError, tarfile.TarError, ValueError)):
        b.pull([sys.executable, "-c", "import sys; sys.exit(255)"], tmp_path / "out", now=START)
    assert [p.name for p in (tmp_path / "out").iterdir()] == [".lock"]


def archive(items):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize("items", [
    [("../evil", b"x")], [("/abs", b"x")], [("events.db", b"x")],
    [("manifest.json", b"{}")], [("manifest.json", b'{"events.db":{}, "../x":{}}')],
])
def test_untrusted_archive_is_rejected(tmp_path, items):
    with pytest.raises(ValueError):
        b.unpack(archive(items), tmp_path)
    assert not (tmp_path.parent / "evil").exists()


def test_wrong_checksum_is_rejected(pi, tmp_path):
    path = b.pull(command(pi), tmp_path / "out", now=START)
    with pytest.raises(ValueError, match="сума"):
        b.verify(path, {"events.db": {"sha256": "0" * 64}})


def test_keeps_recent_and_never_touches_foreign_files(pi, tmp_path):
    out = tmp_path / "out"
    (out / "notes").mkdir(parents=True)
    first = None
    for day in range(4):
        path = b.pull(command(pi), out, keep=2, now=START + timedelta(days=day))
        if first is None:
            first = path
            (path / "mine.txt").write_text("x")
    names = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert len([n for n in names if b.STAMP.fullmatch(n)]) == 3   # 2 свіжі + каталог із чужим файлом
    assert (first / "mine.txt").exists() and (out / "notes").is_dir()


def test_parallel_backup_is_refused(pi, tmp_path):
    import fcntl
    out = tmp_path / "out"
    out.mkdir()
    with open(out / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="уже"):
            b.pull(command(pi), out, now=START)


def test_host_and_root_cannot_become_options_or_shell():
    with pytest.raises(SystemExit):
        b.main(["--host", "-oProxyCommand=x"])
    with pytest.raises(SystemExit):
        b.main(["--host", "a;b"])
    assert b.ssh_command("pi", "a; rm x")[-1] == "python3 -I - 'a; rm x'"


def test_restored_copy_passes_project_restore(tmp_path):
    from server.database_lifecycle import restore_database
    from shared.event_store import init_db
    root = tmp_path / "hub"
    root.mkdir()
    init_db(root / "events.db")
    path = b.pull(command(root), tmp_path / "out", now=START)
    init_db(tmp_path / "live.db")
    restore_database(tmp_path / "live.db", path / "events.db", tmp_path / "copies")


def test_interrupted_copy_is_cleaned_next_time(pi, tmp_path):
    (tmp_path / "out" / ".partial-old").mkdir(parents=True)
    b.pull(command(pi), tmp_path / "out", now=START)
    assert not list((tmp_path / "out").glob(".partial-*"))


def test_scheduled_skips_fresh_copy_and_retries_old(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(b, "pull", lambda command, out: calls.append(1) or tmp_path)
    args = ["--host", "pi@test", "--out", str(tmp_path), "--scheduled"]
    (tmp_path / (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")).mkdir()
    assert b.main(args) == 0 and not calls
    for path in list(tmp_path.iterdir()):
        path.rmdir()
    (tmp_path / (datetime.now(timezone.utc) - timedelta(hours=21)).strftime("%Y%m%dT%H%M%SZ")).mkdir()
    assert b.main(args) == 0 and calls


def test_stale_warning_once_a_day_and_not_for_fresh(tmp_path):
    said = []
    assert not b.warn_stale(tmp_path, said.append)   # щойно, без установки
    old = tmp_path / (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y%m%dT%H%M%SZ")
    old.mkdir()
    assert b.warn_stale(tmp_path, said.append) and not b.warn_stale(tmp_path, said.append)
    assert len(said) == 1 and "4" in said[0]
    old.rename(tmp_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    (tmp_path / ".warned").unlink()
    assert not b.warn_stale(tmp_path, said.append)


def test_agent_does_not_depend_on_documents(tmp_path):
    data = b.agent_plist("pi@test", "hub", tmp_path / "out", tmp_path / "s.py", tmp_path / "log")
    assert ".venv" not in data["ProgramArguments"][0] and data["ProgramArguments"][-1] == "--scheduled"
    assert data["Umask"] == 0o077 and b.OUT.is_relative_to(b.Path.home() / "Library")


def test_agent_script_runs_alone_without_project(tmp_path):
    import shutil, subprocess
    script = tmp_path / "alone.py"
    shutil.copyfile(b.__file__, script)
    done = subprocess.run([sys._base_executable, "-I", str(script), "--help"], cwd=tmp_path, capture_output=True)
    assert done.returncode == 0


def test_overview_for_panel_counts_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "agent_paths", lambda label=b.LABEL: (tmp_path / "agent.plist", None, None))
    assert b.overview(tmp_path / "none") == {"enabled": False, "count": 0, "bytes": 0, "age_h": None, "keep": b.KEEP}
    assert not (tmp_path / "none").exists()
    copy = tmp_path / "out" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    copy.mkdir(parents=True)
    (copy / "events.db").write_bytes(b"x" * 10)
    (tmp_path / "out" / "чуже").mkdir()
    (tmp_path / "agent.plist").touch()
    data = b.overview(tmp_path / "out")
    assert data["enabled"] and data["count"] == 1 and data["bytes"] == 10 and data["age_h"] < 1 and data["keep"] == 3
