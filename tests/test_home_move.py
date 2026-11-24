"""Перенесення дому: справжні тимчасові SQLite, Pi — сусідня тека; збій на будь-якому кроці повертає джерело."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import devices, home_move as hm
from shared import home_marker
from shared.instance_lock import OWNER_LOCK, acquire_instance_lock
from shared.paths import place


RELATIVE = {"events.db": "events.db", "index.sqlite3": ".local-recordings/index.sqlite3"}   # старе місце бази


def database(root, value):
    (root / ".local-recordings").mkdir(parents=True, exist_ok=True)
    for relative in RELATIVE.values():
        db = sqlite3.connect(root / relative)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(v)")
        db.execute("INSERT INTO t VALUES(?)", (value,))
        db.commit()
        db.close()


def value(root, relative="events.db"):
    db = sqlite3.connect(root / relative)
    try:
        return [v for (v,) in db.execute("SELECT v FROM t")]
    finally:
        db.close()


class Side:
    """Пристрій у тимчасовій теці: процеси — прапорці, база справжня."""

    def __init__(self, name, root, alive, audio=True):
        self.name, self.root, self.audio, self.up, self.fail = name, root, audio, True, set()
        self.alive, self.log, self.blocked = set(alive), [], set()

    def state(self):
        if not self.up:
            return {"reachable": False, "error": "SSH недоступний"}
        names = ("node", "asr", "tunnel", "api", "panel")
        return {"reachable": True, "audio": self.audio, "queue": 0, "disk_free": 10 ** 12, "code": devices.code_hash(),
                "processes": {n: {"alive": n in self.alive, "blocked": n in self.blocked} for n in names}}

    def control(self, process, verb):
        self.log.append((verb, process))
        if (verb, process) in self.fail or not self.up:
            raise devices.Refused("синтетичний збій")
        (self.alive.discard if verb == "stop" else self.alive.add)(process)
        if verb == "start" and home_marker.foreign(self.root, self.name) and process in ("asr", "api"):
            self.alive.discard(process)
            self.blocked.add(process)

    def snapshot(self, folder):
        if "snapshot" in self.fail:
            raise hm.MoveError("знімок")
        for name, relative in RELATIVE.items():
            hm.copy_database(self.root / relative, folder / name)

    def install(self, folder, stamp, epoch):
        if "install" in self.fail:
            raise hm.MoveError("запис")
        hm.install_local(self.root, folder, stamp)
        self.mark(self.name, epoch)

    def mark(self, home, epoch):
        if "mark" in self.fail or not self.up:
            raise hm.MoveError("позначка")
        home_marker.write(self.root, home, epoch)


@pytest.fixture
def world(tmp_path, monkeypatch):
    mac_root, pi_root = tmp_path / "mac", tmp_path / "pi"
    for root in (mac_root, pi_root):
        root.mkdir()
    database(mac_root, "mac")
    database(pi_root, "pi-old")
    monkeypatch.setattr(devices, "code_hash", lambda root=None: "code")
    mac, pi = Side("mac", mac_root, {"node", "asr", "api", "tunnel", "panel"}), Side("pi", pi_root, {"panel"})
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
    move = hm.HomeMove(mac_root, mac, pi, sleep=sleep, clock=lambda: now[0], backups=tmp_path / "copies")
    return SimpleNamespace(mac=mac, pi=pi, move=move, mac_root=mac_root, pi_root=pi_root, copies=tmp_path / "copies")


def run(world, target, emergency=False):
    world.move.run(target, emergency)
    return world.move.read()


def test_mac_to_pi_moves_database_services_and_marks(world):
    assert run(world, "pi")["phase"] == "done"
    assert value(world.pi_root) == ["mac"] and value(world.pi_root, RELATIVE["index.sqlite3"]) == ["mac"]
    assert world.mac.alive == {"panel"} and world.pi.alive == {"panel", "node", "asr", "api", "tunnel"}
    assert home_marker.read(world.mac_root)["home"] == home_marker.read(world.pi_root)["home"] == "pi"
    saved, = (place(world.pi_root, ".local-backups")).glob("before-move-*")
    assert value(saved) == ["pi-old"], "стару базу цілі збережено"


def test_round_trip_brings_changes_back(world):
    run(world, "pi")
    db = sqlite3.connect(world.pi_root / "events.db")
    db.execute("INSERT INTO t VALUES('зміна на pi')")
    db.commit()
    db.close()
    time.sleep(1.1)   # мітка копії — до секунди
    assert run(world, "mac")["phase"] == "done"
    assert value(world.mac_root) == ["mac", "зміна на pi"]
    assert "asr" in world.mac.alive and not {"asr", "api", "tunnel", "node"} & world.pi.alive
    assert home_marker.read(world.mac_root) == {"home": "mac", "epoch": 2}


def test_open_panel_connection_survives_install(world):
    panel = sqlite3.connect(world.pi_root / "events.db")   # панель цілі відкрита весь час
    assert panel.execute("SELECT v FROM t").fetchall() == [("pi-old",)]
    panel.commit()
    run(world, "pi")
    assert panel.execute("SELECT v FROM t").fetchall() == [("mac",)]
    panel.close()


def test_air_returns_to_mac_after_pi_without_audio(world):
    world.pi.audio = False
    run(world, "pi")
    assert "node" not in world.mac.alive
    time.sleep(1.1)
    assert run(world, "mac")["phase"] == "done" and "node" in world.mac.alive


def test_pi_without_audio_does_not_start_air(world):
    world.pi.audio = False
    assert run(world, "pi")["phase"] == "done" and "node" not in world.pi.alive


@pytest.mark.parametrize("who,failure", [("mac", "snapshot"), ("pi", "install"), ("pi", ("start", "api")), ("pi", ("start", "asr")),
                                         ("mac", ("stop", "asr"))])
def test_any_failure_returns_source(world, who, failure):
    getattr(world, who).fail.add(failure)
    state = run(world, "pi")
    assert state["phase"] == "rolled_back" and state["error"]
    assert {"node", "asr", "api", "tunnel"} <= world.mac.alive and not {"asr", "api", "tunnel", "node"} & world.pi.alive
    assert home_marker.foreign(world.mac_root, "mac") is None, "джерело знову дім"
    assert value(world.mac_root) == ["mac"]


def test_busy_bot_on_target_rolls_back(world, monkeypatch):
    original = world.pi.control

    def control(process, verb):
        original(process, verb)
        if (verb, process) == ("start", "asr"):
            world.pi.alive.discard("asr")
            world.pi.blocked.add("asr")
    world.pi.control = control
    state = run(world, "pi")
    assert state["phase"] == "rolled_back" and "Зайнято" in state["error"] and "asr" in world.mac.alive


def test_pi_lost_after_install_is_reported_and_mac_resumes(world):
    original = world.pi.install

    def install(*args):
        original(*args)
        world.pi.up = False
    world.pi.install = install
    state = run(world, "pi")
    assert state["phase"] == "failed" and "asr" in world.mac.alive
    assert home_marker.read(world.mac_root)["home"] == "mac"


@pytest.mark.parametrize("setup,text", [
    (lambda w: w.pi.alive.add("asr"), "вже працюють"), (lambda w: w.mac.alive.discard("asr"), "немає чого"),
    (lambda w: setattr(w.pi, "up", False), "SSH"), (lambda w: home_marker.write(w.mac_root, "pi", 3), "Позначка")])
def test_checks_refuse_before_any_change(world, setup, text):
    setup(world)
    assert any(text in p for p in world.move.check("pi")["problems"])
    with pytest.raises(hm.MoveError):
        world.move.start("pi", True)
    assert world.mac.log == [] and world.pi.log == [] and not world.move.state_path.exists()


def test_different_code_and_missing_audio_are_shown(world):
    world.pi.audio = False
    world.pi.state = lambda original=world.pi.state: {**original(), "code": "інший"}
    result = world.move.check("pi")
    assert any("Код" in p for p in result["problems"]) and any("ефіру не буде" in w for w in result["warnings"])


def test_warnings_need_confirmation(world):
    with pytest.raises(hm.MoveError, match="підтвердження"):
        world.move.start("pi")


def copy_of_pi(world, hours):
    from datetime import datetime, timedelta, timezone
    folder = world.copies / (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    for name, relative in RELATIVE.items():
        hm.copy_database(world.pi_root / relative, folder / name)


def test_emergency_uses_last_copy_when_pi_is_dead(world):
    home_marker.write(world.mac_root, "pi", 1)
    world.mac.alive = {"panel"}
    copy_of_pi(world, 5)
    world.pi.up = False
    result = world.move.check("mac")
    assert result["emergency"] and not result["problems"] and any("5 год" in w for w in result["warnings"])
    assert run(world, "mac", emergency=True)["phase"] == "done"
    assert value(world.mac_root) == ["pi-old"] and {"asr", "api", "node"} <= world.mac.alive
    assert home_marker.read(world.mac_root) == {"home": "mac", "epoch": 2}


def test_emergency_without_copy_keeps_mac_database(world):
    home_marker.write(world.mac_root, "pi", 1)
    world.mac.alive = {"panel"}
    world.pi.up = False
    assert any("зі своєю базою" in w for w in world.move.check("mac")["warnings"])
    assert run(world, "mac", emergency=True)["phase"] == "done" and value(world.mac_root) == ["mac"]


def test_interrupted_move_is_seen_and_can_be_returned(world):
    world.move.current = {"id": "x", "source": "mac", "target": "pi", "running": ["node", "asr", "api"], "epoch": 1,
                          "emergency": False, "error": None}
    world.move._save(phase="installing")
    world.mac.alive = {"panel"}
    assert world.move.read()["interrupted"]
    with pytest.raises(hm.MoveError, match="перерване"):
        world.move.start("pi", True)
    world.move.rollback()
    world.move.thread.join(10)
    assert world.move.read()["phase"] == "rolled_back" and {"node", "asr", "api"} <= world.mac.alive


def test_install_waits_for_recognition_to_release_database(tmp_path):
    database(tmp_path, "mac")
    lock = acquire_instance_lock(place(tmp_path, OWNER_LOCK))
    try:
        with pytest.raises(hm.MoveError, match="тримає базу"):
            hm.MacSide(tmp_path).install(tmp_path, "x", 1)
    finally:
        os.close(lock)


def remote(script, home, *args):
    return subprocess.run([sys.executable, "-I", "-", "hub", *args], input=script, capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin"})


def test_remote_install_script(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()
    database(root, "pi-old")
    source = tmp_path / "src"
    database(source, "mac")
    staging = place(root, hm.STAGING) / "S1"
    staging.mkdir(parents=True)
    for name, relative in RELATIVE.items():
        hm.copy_database(source / relative, staging / name)
    sums = json.dumps({n: hm.sha256(staging / n) for n in hm.FILES})
    assert remote(hm.REMOTE_INSTALL, tmp_path, "S1", "4", json.dumps({n: "0" for n in hm.FILES})).stderr.strip() == "checksum"
    lock = acquire_instance_lock(place(root, OWNER_LOCK))
    assert remote(hm.REMOTE_INSTALL, tmp_path, "S1", "4", sums).stderr.strip() == "busy"
    os.close(lock)
    assert value(root) == ["pi-old"]
    done = remote(hm.REMOTE_INSTALL, tmp_path, "S1", "4", sums)
    assert done.returncode == 0, done.stderr
    assert value(root) == ["mac"] and value(place(root, ".local-backups") / "before-move-S1") == ["pi-old"]
    assert home_marker.read(root) == {"home": "pi", "epoch": 4} and not staging.exists()
    assert remote(hm.REMOTE_MARK, tmp_path, "mac", "5").returncode == 0
    assert home_marker.read(root) == {"home": "mac", "epoch": 5}


def test_pi_errors_are_explained():
    def run_(code, err):
        return lambda *a, **k: SimpleNamespace(returncode=code, stdout="", stderr=err)
    for err, text in (("busy\n", "тримає"), ("checksum\n", "пошкоджено"), ("integrity\n", "перевірку"), ("", "не виконала"),
                      ("Traceback\nValueError: x\n", "не виконала")):
        with pytest.raises(hm.MoveError, match=text):
            hm.PiSide("pi@test", "/x", run=run_(1, err))._python("x")


def test_marker_is_validated(tmp_path):
    assert home_marker.read(tmp_path) is None and home_marker.foreign(tmp_path, "mac") is None
    for bad in ("{", '{"home":"x","epoch":1}', '{"home":"pi","epoch":"1"}', "[]"):
        (place(tmp_path, home_marker.NAME)).write_text(bad)
        assert home_marker.foreign(tmp_path, "mac") is None
    home_marker.write(tmp_path, "pi", 2)
    assert home_marker.foreign(tmp_path, "mac") == "pi" and home_marker.foreign(tmp_path, "pi") is None
    assert (place(tmp_path, home_marker.NAME)).stat().st_mode & 0o777 == 0o600
