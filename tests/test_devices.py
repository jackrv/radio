"""Пристрої в панелі: лише читання, недоступна Pi не ламає панель, «дім» визначається за ботом."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from server import devices as d


def device(**alive):
    return {"reachable": True, "processes": {n: {"alive": v} for n, v in alive.items()}, "code": "c"}


@pytest.mark.parametrize("mac,pi,home", [
    (device(asr=True), device(asr=False), "mac"), (device(asr=False), device(asr=True), "pi"),
    (device(asr=True), device(asr=True), "both"), (device(), device(), None),
    (device(), {"reachable": False}, "unknown"), (device(asr=True), {"reachable": False}, "mac"),
    (device(asr=True), None, "mac"), (device(panel=True), device(panel=True), None)])
def test_home_is_where_bot_and_database_run(mac, pi, home):
    assert d.home_of({"mac": mac, "pi": pi}) == home


@pytest.mark.parametrize("value,ok", [("pi@radio345", True), ("radio345", True), ("-oProxyCommand=x", False),
                                      ("a b", False), ("pi@x;rm", False), ("", False)])
def test_host_is_validated(value, ok):
    assert (d.pi_host({"RADIO345_PI_SSH": value}) is not None) is ok


@pytest.mark.parametrize("result", [
    lambda *a, **k: SimpleNamespace(returncode=255, stdout=""), lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
    lambda *a, **k: SimpleNamespace(returncode=0, stdout=""), lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]"),
    lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"processes": 1}')])
def test_bad_answers_mean_unreachable(result):
    assert d.remote("pi@test", run=result)["reachable"] is False


def test_timeout_and_missing_ssh_mean_unreachable():
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 1)

    def missing(*a, **k):
        raise FileNotFoundError
    assert not d.remote("pi@test", run=timeout)["reachable"] and not d.remote("pi@test", run=missing)["reachable"]


def test_remote_command_is_fixed_and_batch():
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"processes": {}}))
    assert d.remote("pi@test", run=run)["reachable"]
    assert seen["command"][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"] and seen["command"][5:7] == ["--", "pi@test"]
    assert seen["kwargs"]["timeout"] == d.SSH_TIMEOUT_S


@pytest.mark.skipif(sys.platform != "darwin" and not sys.platform.startswith("linux"), reason="POSIX")
def test_remote_script_reads_database_and_never_writes(tmp_path):
    import sqlite3
    home, root = tmp_path, tmp_path / "hub"
    (root / "deploy/profiles").mkdir(parents=True)
    (root / "deploy/profiles/pi.files").write_text("# x\n/a.py\n")
    (root / "a.py").write_text("1")
    db = sqlite3.connect(root / "events.db")
    db.execute("CREATE TABLE radio_events(ts TEXT)")
    db.execute("INSERT INTO radio_events VALUES('2026-11-24 10:00:00')")
    db.commit()
    db.close()
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "systemctl"
    fake.write_text("#!/bin/sh\necho ActiveState=active; echo UnitFileState=enabled; echo NRestarts=2\n")
    fake.chmod(0o755)
    before = {p.name: p.stat().st_mtime_ns for p in root.iterdir()}
    done = subprocess.run([sys.executable, "-", "hub", "asr"], input=d.REMOTE, capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": f"{tmp_path}/bin:/usr/bin:/bin"})
    data = json.loads(done.stdout)
    assert data["events"] == 1 and data["last_event"] and data["processes"]["asr"] == {"alive": True, "enabled": True, "restarts": 2, "blocked": False}
    assert data["code"] == d.code_hash(root) and data["queue"] == 0
    assert before == {p.name: p.stat().st_mtime_ns for p in root.iterdir()}


def test_result_is_cached_and_pi_failure_does_not_break_the_page(monkeypatch, tmp_path):
    calls, clock = [], [0.0]
    monkeypatch.setattr(d, "local", lambda db, root: device(asr=True))

    def fetch(host):
        calls.append(host)
        return {"reachable": False, "error": "SSH недоступний"}
    state = d.Devices("x", tmp_path, "pi@test", fetch, clock=lambda: clock[0])
    assert state.read()["home"] == "mac" and state.read()["code_matches"] is None and len(calls) == 1
    clock[0] = d.FRESH_S
    state.read()
    assert len(calls) == 2


def test_without_host_pi_is_not_contacted(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "local", lambda db, root: device(asr=True))
    state = d.Devices("x", tmp_path, None, lambda host: pytest.fail("звернення до Pi"))
    assert state.read()["pi"] is None and state.read()["configured"] is False
