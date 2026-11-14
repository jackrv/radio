"""Справжні тестові процеси; робочі служби, база й порти Radio 345 не використовуються."""

import json
import os
from pathlib import Path
import pty
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

import start_radio345 as launcher
from core.instance_lock import acquire_instance_lock
from services.launcher_lifecycle import LauncherSession, lock_held, managed_status, read_state, request_stop

FIXTURE = Path(__file__).with_name("launcher_fixture.py")
REPO = Path(__file__).resolve().parents[1]


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("test process did not reach expected state")


def available(port):
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture
def environment(tmp_path):
    sockets = [socket.socket() for _ in range(3)]
    for server in sockets:
        server.bind(("127.0.0.1", 0))
    ports = [server.getsockname()[1] for server in sockets]
    for server in sockets:
        server.close()
    parents = []
    logs = []

    def start(*flags, tty=False):
        for name in ("api", "ngrok", "radio", "parent", "startup", "registration"):
            (tmp_path / f"{name}.ready").unlink(missing_ok=True)
        command = [sys.executable, str(FIXTURE), "parent", str(tmp_path), *map(str, ports), *flags]
        master = slave = None
        if tty:
            master, slave = pty.openpty()
            command.append("tty")
            stdout = slave
        else:
            stdout = (tmp_path / f"parent-{len(parents)}.log").open("w")
            logs.append(stdout)
        proc = subprocess.Popen(command, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                                stdin=slave if tty else subprocess.DEVNULL, stdout=stdout,
                                stderr=subprocess.STDOUT, start_new_session=not tty)
        if slave is not None:
            os.close(slave)
        parents.append(proc)
        ready_file = "registration.ready" if "interrupt-register" in flags else "startup.ready" if "slow" in flags else "parent.ready"
        until(lambda: (tmp_path / ready_file).exists())
        return proc, master

    yield tmp_path, ports, start
    for parent in parents:
        if parent.poll() is None:
            parent.terminate()
            try:
                parent.wait(timeout=8)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=2)
    # Лише групи, створені цим тестом, якщо сам тест перервався до очищення.
    if lock_held(tmp_path / ".radio345-launcher/lock") or not all(available(p) for p in ports):
        for pid in read_state(tmp_path).get("controllers", {}).values():
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for file in logs:
        file.close()


def assert_clean(root, ports, *, normal=True):
    until(lambda: not lock_held(root / ".radio345-launcher/lock"))
    until(lambda: all(available(port) for port in ports))
    assert not lock_held(root / ".radio345.lock")
    with sqlite3.connect(root / "test-only.db") as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT COUNT(*) FROM result").fetchone()[0] == (1 if normal else 0)
    if normal:
        events = (root / "events.txt").read_text().splitlines()
        assert events.index("radio.queue_drained") < events.index("radio.done")
        assert events.index("radio.done") < events.index("ngrok.stopping")
        assert events.index("ngrok.done") < events.index("api.stopping")


@pytest.mark.parametrize("signo", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
def test_parent_exit_stops_owned_services_in_order_and_allows_restart(environment, signo):
    root, ports, start = environment
    parent, _ = start()
    inode = (root / ".radio345.lock").stat().st_ino
    parent.send_signal(signo)
    parent.wait(timeout=10)
    assert_clean(root, ports)
    assert (root / ".radio345.lock").stat().st_ino == inode
    (root / "events.txt").unlink()
    with sqlite3.connect(root / "test-only.db") as database:
        database.execute("DELETE FROM result")
    second, _ = start()
    second.send_signal(signal.SIGINT)
    second.wait(timeout=10)
    assert_clean(root, ports)


def test_closing_terminal_hangs_up_launcher_and_leaves_no_services(environment):
    root, ports, start = environment
    parent, master = start(tty=True)
    os.close(master)
    parent.wait(timeout=10)
    assert_clean(root, ports)


def test_parent_killed_during_partial_startup_does_not_wait_for_unstarted_services(environment):
    root, ports, start = environment
    parent, _ = start("slow")
    parent.kill()
    parent.wait(timeout=5)
    until(lambda: not lock_held(root / ".radio345-launcher/lock"))
    until(lambda: available(ports[0]))
    assert not (root / "radio.ready").exists()


def test_interruption_between_popen_and_registration_in_parent_list_is_cleaned(environment):
    root, ports, start = environment
    parent, _ = start("interrupt-register")
    parent.wait(timeout=10)
    until(lambda: not lock_held(root / ".radio345-launcher/lock"))
    assert all(available(port) for port in ports)


def test_stop_uses_authenticated_channel_and_does_not_signal_foreign_process(environment):
    root, ports, start = environment
    foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    try:
        parent, _ = start()
        status = managed_status(root)
        assert status["managed"] and set(status["controllers"]) == {"api", "ngrok", "radio"}
        assert "token" not in json.dumps(status)
        assert request_stop(root, timeout=10)
        parent.wait(timeout=5)
        assert foreign.poll() is None
        assert_clean(root, ports)
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


def test_hung_service_is_killed_within_its_deadline_and_sqlite_is_consistent(environment):
    root, ports, start = environment
    parent, _ = start("hung")
    parent.kill()
    parent.wait(timeout=5)
    assert_clean(root, ports, normal=False)


def test_dead_controller_is_cleaned_using_its_own_live_completion_pipe(environment):
    root, ports, start = environment
    parent, _ = start()
    pid = read_state(root)["controllers"]["radio"]
    os.kill(pid, signal.SIGKILL)
    parent.wait(timeout=10)
    assert_clean(root, ports)


def test_status_without_environment_or_database_is_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "port_busy", lambda _: False)
    monkeypatch.setattr(sys, "argv", ["start_radio345.py", "--status"])
    assert launcher.main() == 0
    assert list(tmp_path.iterdir()) == []
    assert "вільний" in capsys.readouterr().out


def test_stop_refuses_unmanaged_processes_and_does_not_remove_receiver_lock(tmp_path):
    lock = acquire_instance_lock(tmp_path / ".radio345.lock")
    try:
        assert request_stop(tmp_path) is False
        assert lock_held(tmp_path / ".radio345.lock")
    finally:
        os.close(lock)
    assert (tmp_path / ".radio345.lock").exists()


def test_second_launcher_cannot_replace_control_state(tmp_path):
    with LauncherSession(tmp_path, ["api"]):
        original = read_state(tmp_path)
        with pytest.raises(RuntimeError, match="Інший запускатель"):
            with LauncherSession(tmp_path, ["api"]):
                pass
        assert read_state(tmp_path) == original


def test_stale_stop_token_cannot_stop_the_current_session(environment):
    root, ports, start = environment
    parent, _ = start()
    fifo = root / ".radio345-launcher/control.fifo"
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, ("stop " + "0" * 32 + "\n").encode())
    finally:
        os.close(fd)
    time.sleep(0.15)
    assert parent.poll() is None
    assert managed_status(root)["launcher_reachable"]
    parent.send_signal(signal.SIGTERM)
    parent.wait(timeout=10)
    assert_clean(root, ports)
