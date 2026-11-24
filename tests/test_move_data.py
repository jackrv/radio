"""Перенесення даних: лише коли все зупинено, без втрат і перезапису; код бачить обидва місця."""
import os
import sqlite3

import pytest

from shared import move_data as m
from shared.instance_lock import acquire_instance_lock
from shared.paths import DATA, NAMES, place, root_of


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".local-recordings").mkdir()
    for path in ("events.db", ".local-recordings/index.sqlite3"):
        db = sqlite3.connect(tmp_path / path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(v)")
        db.execute("INSERT INTO t VALUES(7)")
        db.commit()
        db.close()
    (tmp_path / ".local-recordings/a.wav").write_bytes(b"x")
    for name in ("radio.log", "radio-1.log", ".radio345.lock", ".local-home.json"):
        (tmp_path / name).write_text("x")
    (tmp_path / ".env").write_text("S=1")
    (tmp_path / "config.py").write_text("")
    return tmp_path


def value(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT v FROM t").fetchone()[0]
    finally:
        db.close()


def test_everything_moves_and_code_finds_it(root):
    assert place(root, "events.db") == root / "events.db", "до перенесення — старе місце"
    done = m.move(root, ports=())
    assert {"events.db", ".local-recordings", "radio.log", "radio-1.log", ".radio345.lock"} <= set(done)
    assert value(place(root, "events.db")) == 7 and value(place(root, ".local-recordings") / "index.sqlite3") == 7
    assert (root / DATA / "recordings/a.wav").exists() and (root / DATA / "model.lock").exists()
    assert sorted(p.name for p in root.iterdir()) == [".env", DATA, "config.py"], "чуже не чіпаємо"
    assert root_of(place(root, "events.db")) == root and (root / DATA).stat().st_mode & 0o777 == 0o700
    assert m.move(root, ports=()) == []


@pytest.mark.parametrize("name", m.LOCKS)
def test_running_service_blocks_move(root, name):
    lock = acquire_instance_lock(root / name)
    try:
        with pytest.raises(RuntimeError, match="зупиніть"):
            m.move(root, ports=())
    finally:
        os.close(lock)
    assert (root / "events.db").exists() and not (root / DATA / "events.db").exists()


def test_open_database_blocks_move(root):
    db = sqlite3.connect(root / "events.db")
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO t VALUES(8)")
    try:
        with pytest.raises((RuntimeError, sqlite3.Error)):
            m.move(root, ports=())
    finally:
        db.rollback()
        db.close()
    assert value(root / "events.db") == 7 and not (root / DATA / "events.db").exists()


def test_existing_target_is_not_overwritten(root):
    (root / DATA).mkdir()
    (root / DATA / "events.db").write_text("інше")
    with pytest.raises(RuntimeError, match="не перезаписую"):
        m.move(root, ports=())
    assert value(root / "events.db") == 7 and (root / DATA / "events.db").read_text() == "інше"


def test_interrupted_move_continues(root):
    (root / DATA).mkdir()
    os.rename(root / ".local-recordings", root / DATA / "recordings")
    m.move(root, ports=())
    assert value(place(root, "events.db")) == 7 and (root / DATA / "recordings/a.wav").exists()


def test_names_are_unique_and_ignored_by_git():
    assert len(set(NAMES.values())) == len(NAMES)
    ignore = (m.Path(m.__file__).resolve().parent.parent / ".gitignore").read_text()
    assert ".local-data/" in ignore


def test_diagnostic_history_is_not_nested(tmp_path):
    from shared.diagnostic_history import history_directory
    assert history_directory(tmp_path / DATA / "diagnostics.json") == tmp_path / DATA / "diagnostics"
    assert not (tmp_path / DATA / DATA).exists()
