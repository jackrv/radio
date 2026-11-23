"""Запуск за профілем: процеси незалежні, бот лише де дозволено, замки моделі й бази різні."""
import os
import sys
import threading
import time

import pytest

import radio345
from core.instance_lock import OWNER_LOCK, acquire_instance_lock
from tools import components, profiles

SLEEP = ("-c", "import time; print('up', flush=True); time.sleep(60)")
CRASH = ("-c", "import sys; sys.exit(3)")
STUBBORN = ("-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)")


def child(name, command, lines):
    return radio345.Child(name, command, dict(os.environ), lambda n, line: lines.append((n, line)))


def wait(check, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def running(children, stop):
    thread = threading.Thread(target=radio345.supervise, args=(children, stop, 0.05), daemon=True)
    thread.start()
    return thread


def test_crash_of_one_process_does_not_touch_the_other(monkeypatch):
    monkeypatch.setattr(radio345, "MIN_PAUSE_S", 0.1)
    lines, stop = [], threading.Event()
    good, bad = child("node", SLEEP, lines), child("panel", CRASH, lines)
    good.pause = bad.pause = 0.1
    thread = running([good, bad], stop)
    try:
        assert wait(lambda: bad.restarts >= 2) and wait(lambda: ("node", "up") in lines)
        assert good.restarts == 0 and good.process.poll() is None
        assert any(n == "panel" and "код 3" in line for n, line in lines)
    finally:
        stop.set()
        thread.join(30)
    assert good.process.poll() is not None


def test_killed_process_is_restarted():
    lines, stop = [], threading.Event()
    one = child("node", SLEEP, lines)
    one.pause = 0.1
    thread = running([one], stop)
    try:
        assert wait(lambda: ("node", "up") in lines)
        one.process.kill()
        assert wait(lambda: one.restarts == 1)
    finally:
        stop.set()
        thread.join(30)


def test_stubborn_process_is_killed_on_stop(monkeypatch):
    monkeypatch.setattr(radio345, "STOP_S", 0.5)
    lines, stop = [], threading.Event()
    one = child("asr", STUBBORN, lines)
    thread = running([one], stop)
    assert wait(lambda: ("asr", "up") in lines)
    stop.set()
    thread.join(15)
    assert not thread.is_alive() and one.process.poll() == -9


def test_backoff_grows_and_resets_after_stable_run():
    now, lines = [0.0], []

    class Done:
        def poll(self):
            return 1

    one = radio345.Child("x", SLEEP, {}, lambda *a: lines.append(a), clock=lambda: now[0])
    one.process, one.started = Done(), 0.0
    one.tick()
    assert one.pause == 4.0 and one.next_start == 2.0
    one.process, one.started, now[0] = Done(), 100.0, 200.0   # працював довше STABLE_S
    one.tick()
    assert one.next_start == 202.0


def test_bot_token_reaches_asr_only_in_a_bot_profile():
    env = {"TELEGRAM_BOT_TOKEN": "синтетичний"}
    assert radio345.process_environment("asr", {"bot": False}, env)["TELEGRAM_BOT_TOKEN"] == ""
    assert radio345.process_environment("asr", {"bot": True}, env)["TELEGRAM_BOT_TOKEN"]
    assert radio345.process_environment("panel", {"bot": False}, env)["TELEGRAM_BOT_TOKEN"]


def test_empty_token_is_not_restored_from_dotenv(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=синтетичний\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TELEGRAM_BOT_TOKEN"] == ""


def test_environment_priority():
    profile = {"env": {"A": "профіль", "B": "профіль", "C": "профіль"}}
    env = radio345.base_environment(profile, {"A": "оточення"}, {"A": "env", "B": "env"})
    assert (env["A"], env["B"], env["C"]) == ("оточення", "env", "профіль")


@pytest.mark.parametrize("name", profiles.names())
def test_every_profile_process_has_a_command_with_existing_module(name):
    profile = radio345.load_profile(name)
    names = radio345.selected(profile)
    assert set(names) == set(profile["processes"]) - {"lab"}
    for process in names:
        assert radio345.COMMANDS[process][1] in components.modules()


def test_unknown_profile_process_and_missing_env():
    with pytest.raises(SystemExit):
        radio345.load_profile("немає")
    with pytest.raises(SystemExit):
        radio345.selected(radio345.load_profile("mac-worker"), ["panel"])
    pi = radio345.load_profile("pi")
    assert radio345.missing_env(pi, {}) == ["RADIO_ASR_URL"]
    assert radio345.missing_env(pi, {}, ["panel"]) == []


def test_model_and_owner_locks_are_independent(tmp_path):
    model = acquire_instance_lock(tmp_path / ".radio345.lock")
    owner = acquire_instance_lock(tmp_path / OWNER_LOCK)
    try:
        assert model is not None and owner is not None
        assert acquire_instance_lock(tmp_path / OWNER_LOCK) is None
    finally:
        os.close(model)
        os.close(owner)


def test_restore_is_refused_while_recognition_service_runs(tmp_path):
    from services.database_lifecycle import restore_database
    from services.event_store import init_db
    init_db(tmp_path / "events.db")
    init_db(tmp_path / "copy.db")
    owner = acquire_instance_lock(tmp_path / OWNER_LOCK)
    try:
        with pytest.raises(RuntimeError, match="службу розпізнавання"):
            restore_database(tmp_path / "events.db", tmp_path / "copy.db", tmp_path / "b")
    finally:
        os.close(owner)
    assert (fd := acquire_instance_lock(tmp_path / ".radio345.lock")) is not None
    os.close(fd)
