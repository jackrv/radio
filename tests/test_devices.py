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


def ssh(code=0, stderr=""):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=code, stdout="", stderr=stderr)
    run.seen = seen
    return run


@pytest.mark.parametrize("verb,args", [("start", "enable --now"), ("stop", "disable --now"), ("restart", "restart")])
def test_pi_commands_are_exact_and_allowed_by_the_rule(verb, args):
    run = ssh()
    d.control_pi("pi@test", "asr", verb, run=run)
    assert run.seen[0][-1] == f"sudo -n /usr/bin/systemctl {args} radio345-asr"
    script = (d.ROOT / "deploy/hub/control.sh").read_text(encoding="utf-8")
    assert "for name in " + " ".join(d.PI_CONTROLLED) in script
    assert f'"{args}"' in script or args in script
    assert "*" not in script.split("for name in")[1].split("done >")[0]


@pytest.mark.parametrize("process,verb", [("ssh", "stop"), ("asr", "mask"), ("asr; rm", "stop"), ("worker", "stop")])
def test_pi_refuses_unknown_actions_without_ssh(process, verb):
    run = ssh()
    with pytest.raises(d.Refused):
        d.control_pi("pi@test", process, verb, run=run)
    assert run.seen == []


@pytest.mark.parametrize("code,err,text", [(255, "", "SSH"), (1, "sudo: a password is required", "control.sh"), (1, "x", "не виконала")])
def test_pi_failures_are_explained(code, err, text):
    with pytest.raises(d.Refused, match=text):
        d.control_pi("pi@test", "node", "stop", run=ssh(code, err))


def actions(monkeypatch, tmp_path, mac, pi):
    monkeypatch.setattr(d, "local", lambda db, root: mac)
    state, done = d.Devices("x", tmp_path, "pi@test", lambda host: pi), []
    return (lambda *a: state.act(*a, control_pi=lambda h, p, v: done.append(("pi", p, v)),
                                 control_mac=lambda p, v, r: done.append(("mac", p, v)))), done


@pytest.mark.parametrize("process", ["asr", "tunnel"])
@pytest.mark.parametrize("verb", ["start", "restart"])
def test_second_bot_or_tunnel_is_refused(monkeypatch, tmp_path, process, verb):
    act, done = actions(monkeypatch, tmp_path, device(**{process: True}), device(**{process: False}))
    with pytest.raises(d.Refused, match="іншому пристрої"):
        act("pi", process, verb)
    assert done == []
    act("pi", process, "stop")
    act("pi", "node", "start")
    act("mac", process, "restart")
    assert done == [("pi", process, "stop"), ("pi", "node", "start"), ("mac", process, "restart")]


def test_unreachable_pi_does_not_block_mac(monkeypatch, tmp_path):
    act, done = actions(monkeypatch, tmp_path, device(asr=False), {"reachable": False})
    act("mac", "asr", "start")
    assert done == [("mac", "asr", "start")]


def test_mac_panel_cannot_stop_itself_and_needs_r345(tmp_path):
    with pytest.raises(d.Refused, match="Невідома"):
        d.control_mac("panel", "stop", tmp_path)
    with pytest.raises(d.Refused, match="не r345"):
        d.control_mac("asr", "stop", tmp_path)


@pytest.mark.parametrize("mode,marker,mac,pi,expected", [
    ("pi", "pi", {"node", "tunnel"}, {"tunnel"},
     ["mac node stop", "mac tunnel stop", "mac worker start", "pi api start", "pi asr start", "pi node start"]),
    ("mac", "mac", {"worker", "tunnel"}, {"node"}, ["pi node stop", "mac api start", "mac asr start", "mac node start"]),
    ("off", "pi", {"worker"}, {"node", "asr", "panel"}, ["mac worker stop", "pi node stop", "pi asr stop"])])
def test_mode_starts_needed_and_stops_extra(monkeypatch, tmp_path, mode, marker, mac, pi, expected):
    names = ("node", "asr", "api", "tunnel", "worker", "panel")
    state, calls = mode_state(monkeypatch, tmp_path, marker,
                              {n: {"alive": n in mac} for n in names}, {n: {"alive": n in pi} for n in names if n != "worker"})
    assert state.apply_mode(mode, *recorder(calls)) is None
    assert calls == expected


def test_mode_never_moves_home_and_reports_lost_pi(monkeypatch, tmp_path):
    state, calls = mode_state(monkeypatch, tmp_path, "mac", {"worker": {"alive": True}}, None)
    with pytest.raises(d.Refused, match="перенесіть"):
        state.apply_mode("pi", *recorder(calls))
    assert calls == []
    assert "Pi недоступна" in state.apply_mode("off", *recorder(calls)) and calls == ["mac worker stop"]


def test_current_mode():
    off, on = {"reachable": True, "processes": {}}, {"reachable": True, "processes": {"node": {"alive": True}}}
    home = {"home": "mac", "epoch": 1}
    assert d.mode_of({"mac": off, "pi": off}, home) == "off"
    assert d.mode_of({"mac": off, "pi": on}, home) == "mac"
    assert d.mode_of({"mac": off, "pi": {"reachable": False}}, home) == "mac"


def mode_state(monkeypatch, tmp_path, marker, mac, pi):
    from shared import home_marker
    home_marker.write(tmp_path, marker, 1)
    monkeypatch.setattr(d, "local", lambda db, root: {"reachable": True, "code": "c", "processes": mac})
    remote = {"reachable": False} if pi is None else {"reachable": True, "processes": pi}
    return d.Devices(tmp_path / "e.db", tmp_path, host="u@pi", fetch=lambda host: remote), []


def recorder(calls):
    return (lambda host, p, verb: calls.append(f"pi {p} {verb}"), lambda p, verb, root: calls.append(f"mac {p} {verb}"))


def test_pi_panel_controls_only_its_own_services(tmp_path):
    run = ssh()
    pi = d.ThisPi(tmp_path, run=run)
    pi.act("pi", "asr", "stop")
    pi.act("pi", "panel", "restart")
    assert run.seen == [["systemctl", "--no-ask-password", "stop", "radio345-asr"],
                        ["systemctl", "--no-block", "--no-ask-password", "restart", "radio345-panel"]]
    for device, process, verb in (("mac", "asr", "stop"), ("pi", "ssh", "stop"), ("pi", "asr", "mask"), ("pi", "panel", "stop")):
        with pytest.raises(d.Refused):
            pi.act(device, process, verb)
    assert len(run.seen) == 2
    with pytest.raises(d.Refused, match="control.sh"):
        d.ThisPi(tmp_path, run=ssh(1, "Interactive authentication required.")).act("pi", "asr", "stop")
    with pytest.raises(d.Refused):
        pi.audio("mac")


def test_pi_panel_reads_its_own_state(tmp_path):
    from shared import home_marker
    home_marker.write(tmp_path, "pi", 1)
    answer = SimpleNamespace(returncode=0, stdout='{"processes": {"asr": {"alive": true}}}\n', stderr="")
    data = d.ThisPi(tmp_path, run=lambda *a, **k: answer).read()
    assert data["self"] == "pi" and data["mac"] is None and data["home"] == data["mode"] == "pi" and "asr" in data["needed"]["pi"]
    broken = d.ThisPi(tmp_path, run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")).read()
    assert broken["pi"]["reachable"] is False


def test_polkit_rule_is_narrow():
    script = (d.ROOT / "deploy/hub/control.sh").read_text(encoding="utf-8")
    rule = script.split("<<RULE")[1].split("RULE")[0]
    assert "manage-units" in rule and "manage-unit-files" not in rule and '["start", "stop", "restart"]' in rule
    assert '"' + '", "'.join(d.PI_CONTROLLED) + '"' in rule
