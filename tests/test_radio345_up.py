"""Запуск за профілем: процеси незалежні, бот лише де дозволено, замки моделі й бази різні."""
import os
import sys
import threading
import time

import pytest

import radio345
from shared.instance_lock import OWNER_LOCK, acquire_instance_lock
from tools import components, profiles
from shared.paths import place

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
    assert good.process is None


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
    monkeypatch.setattr(radio345, "GRACE_S", 0.5)
    lines, stop = [], threading.Event()
    one = child("asr", STUBBORN, lines)
    thread = running([one], stop)
    assert wait(lambda: ("asr", "up") in lines)
    stop.set()
    thread.join(15)
    assert not thread.is_alive() and one.process is None


def test_backoff_grows_and_resets_after_stable_run():
    now, lines = [0.0], []

    class Done:
        pid = 2 ** 22 + 12345   # такої групи немає: _reap нічого не зачепить

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
    assert set(names) == set(profile["processes"]) - {"lab"} - set(profile.get("manual", ()))
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
    model = acquire_instance_lock(place(tmp_path, ".radio345.lock"))
    owner = acquire_instance_lock(place(tmp_path, OWNER_LOCK))
    try:
        assert model is not None and owner is not None
        assert acquire_instance_lock(place(tmp_path, OWNER_LOCK)) is None
    finally:
        os.close(model)
        os.close(owner)


def test_restore_is_refused_while_recognition_service_runs(tmp_path):
    from server.database_lifecycle import restore_database
    from shared.event_store import init_db
    init_db(tmp_path / "events.db")
    init_db(tmp_path / "copy.db")
    owner = acquire_instance_lock(place(tmp_path, OWNER_LOCK))
    try:
        with pytest.raises(RuntimeError, match="службу розпізнавання"):
            restore_database(tmp_path / "events.db", tmp_path / "copy.db", tmp_path / "b")
    finally:
        os.close(owner)
    assert (fd := acquire_instance_lock(place(tmp_path, ".radio345.lock"))) is not None
    os.close(fd)


PARENT = """
import sys, threading, time
sys.path.insert(0, {root!r})
import radio345
child = radio345.Child("node", ("-c", "import time,os; print('pid', os.getpid(), flush=True); time.sleep(60)"),
                       dict(__import__('os').environ), lambda n, line: print(line, flush=True))
child.tick()
time.sleep(60)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_services_stop_when_supervisor_is_killed():
    import subprocess
    parent = subprocess.Popen([sys.executable, "-u", "-c", PARENT.format(root=str(radio345.ROOT))],
                              stdout=subprocess.PIPE, text=True)
    pid = int(parent.stdout.readline().split()[1])
    parent.kill()
    parent.wait()
    assert wait(lambda: not alive(pid)), "служба пережила смерть наглядача"
    parent.stdout.close()


def test_service_does_not_survive_killed_guard():
    lines, stop = [], threading.Event()
    one = child("node", ("-c", "import time,os; print(os.getpid(), flush=True); time.sleep(60)"), lines)
    one.pause = 0.1
    thread = running([one], stop)
    try:
        assert wait(lambda: lines)
        pid = int(lines[0][1])
        one.process.kill()
        assert wait(lambda: not alive(pid)) and wait(lambda: one.restarts == 1)
    finally:
        stop.set()
        thread.join(30)


def test_status_restart_and_stop_of_real_supervisor(tmp_path, monkeypatch):
    """Справжній r345 у копії проєкту: стан, вибірковий перезапуск, зупинка, відмова другому запуску."""
    import shutil
    import subprocess
    for item in ("radio345.py", "shared/process_guard.py", "shared/r345_control.py", "shared/conflict.py", "shared/home_marker.py", "shared/paths.py", "shared/instance_lock.py", "shared/__init__.py"):
        target = tmp_path / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(radio345.ROOT / item, target)
    (tmp_path / "server").mkdir(exist_ok=True)
    (tmp_path / "server/__init__.py").touch()
    for name in ("radio_node", "asr_service", "miniapp_tunnel"):
        (tmp_path / f"server/{name}.py").write_text("import time\ntime.sleep(300)\n")
    (tmp_path / "deploy/profiles").mkdir(parents=True)
    (tmp_path / "deploy/profiles/t.toml").write_text('description="t"\nprocesses=["node","asr","tunnel"]\nmanual=["tunnel"]\nbot=false\n[env]\n')
    (tmp_path / ".env").write_text("RADIO345_PROFILE=t\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("RADIO")}

    def r345(*args):
        return subprocess.run([sys.executable, "-m", "radio345", *args], cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=60)

    assert r345("status").returncode == 3
    up = subprocess.Popen([sys.executable, "-m", "radio345", "up"], cwd=tmp_path, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait(lambda: r345("status").returncode == 0, 20)
        second = r345("up")
        assert second.returncode != 0 and "уже запущено" in second.stderr
        control = radio345.Control(place(tmp_path, ".radio345-launcher"))
        before = control.read()["processes"]
        done = r345("restart", "asr")
        assert done.returncode == 0, done.stdout
        after = control.read()["processes"]
        assert after["asr"]["pid"] != before["asr"]["pid"] and after["node"]["pid"] == before["node"]["pid"]
        assert r345("restart", "panel").returncode == 2
        assert not after["tunnel"]["alive"] and after["tunnel"]["wanted"] is False, "manual сам не стартує"
        control.request("stop", ["asr"])
        assert wait(lambda: not control.read()["processes"]["asr"]["alive"], 20)
        time.sleep(3)
        assert not control.read()["processes"]["asr"]["alive"] and r345("status").returncode == 0, "зупинене навмисно не піднімається"
        control.request("start", ["asr", "tunnel"])
        assert wait(lambda: all(control.read()["processes"][n]["alive"] for n in ("asr", "tunnel")), 20)
        assert control.read()["processes"]["node"]["pid"] == before["node"]["pid"]
        control.request("stop", ["tunnel"])
        assert wait(lambda: not control.read()["processes"]["tunnel"]["alive"], 20)
        assert r345("stop").returncode == 0
        assert up.wait(10) == 0 and r345("status").returncode == 3
    finally:
        if up.poll() is None:
            up.kill()
        up.stdout.close()


def test_foreign_lock_blocks_r345_and_is_not_stopped_by_it(tmp_path, capsys):
    (tmp_path / "l").mkdir()
    old = acquire_instance_lock(tmp_path / "l" / "lock")
    control = radio345.Control(tmp_path / "l")
    try:
        with pytest.raises(SystemExit, match="уже запущено"):
            control.acquire()
        assert control.read() is None and radio345.stop_all(control) == 3
        assert "не відповідає" in capsys.readouterr().out
    finally:
        os.close(old)


def test_plain_up_uses_env_choice_and_respects_home(tmp_path):
    from shared import home_marker
    profile = radio345.load_profile("mac-all")
    chosen = {"RADIO345_ONLY": "worker node asr panel api tunnel"}
    assert "tunnel" not in radio345.default_processes(profile, tmp_path, {}), "без явного вибору публічної адреси немає"
    assert set(radio345.default_processes(profile, tmp_path, chosen)) == {"worker", "node", "asr", "panel", "api", "tunnel"}
    other = "pi" if home_marker.this_device() == "mac" else "mac"
    home_marker.write(tmp_path, other, 1)
    assert set(radio345.default_processes(profile, tmp_path, chosen)) == {"worker"}
    home_marker.write(tmp_path, home_marker.this_device(), 2)
    assert "asr" in radio345.default_processes(profile, tmp_path, chosen)
    with pytest.raises(SystemExit):
        radio345.default_processes(profile, tmp_path, {"RADIO345_ONLY": "невідомий"})


def doctor(monkeypatch, capsys, remote, ios="Встановлено: 1 дн. тому\nПідпис діє до: 29.11.2026 11:03\nАгент: увімкнено\n", rows=None, home=None, seen=None):
    import json
    from types import SimpleNamespace
    from server import devices
    from shared import pull_backup
    monkeypatch.setattr(radio345, "show_status", lambda control, title="": 0)
    monkeypatch.setattr(devices, "pi_host", lambda env: "pi@test")
    monkeypatch.setattr(devices, "remote", lambda host: remote)
    monkeypatch.setattr(devices, "code_hash", lambda: "same")
    monkeypatch.setattr(pull_backup, "overview", lambda: {"count": 1, "keep": 3, "age_h": 5.0})
    monkeypatch.setattr(radio345.sys, "platform", "darwin")
    monkeypatch.setattr(radio345.home_marker, "foreign", lambda root: home)
    out = json.dumps(rows or [["База", "ціла", True]], ensure_ascii=False)

    def run(command, **k):
        if seen is not None:
            seen.append(command)
        return SimpleNamespace(stdout=out if "--rows" in command[-1] else ios)
    code = radio345.doctor(run=run)
    return code, capsys.readouterr().out


def test_doctor_is_aligned_and_complete(monkeypatch, capsys):
    code, out = doctor(monkeypatch, capsys, {"reachable": True, "processes": {"panel": {"alive": True}, "asr": {"alive": False}}, "code": "old"})
    assert code == 0 and "підпис до 29.11.2026 11:03" in out and "Оновити код Pi" in out and "1 з 3, остання 5 год тому" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    starts = {line.index(v) for line, v in zip(lines, ("ціла", "працює: panel", "інший", "підпис до", "увімкнено", "1 з 3"))}
    assert len(starts) == 1, out


def test_doctor_reports_problems(monkeypatch, capsys):
    code, out = doctor(monkeypatch, capsys, {"reachable": False, "error": "SSH недоступний"}, rows=[["База", "ПОШКОДЖЕНА", False]])
    assert code == 1 and "✗ База" in out and "✗ Pi" in out and "SSH недоступний" in out


def test_table_aligns_columns_and_marks():
    text = radio345.table([(True, "node", "працює", ""), (None, "asr-довга", "зупинено", ""), (False, "x", "не працює", "перезапусків 2")])
    first, second, third = text.splitlines()
    assert first.index("працює") == second.index("зупинено") == third.index("не працює") and not first.endswith(" ")
    assert "●" in first and "○" in second and "✗" in third


@pytest.mark.parametrize("words", [[], ["-h"], ["--help"], ["help"]])
def test_help_lists_every_command(words, capsys):
    assert radio345.main(words) == 0
    out = capsys.readouterr().out
    for command in ("up", "check", "status", "stop", "restart", "pause-model", "resume-model", "doctor", "update", "service", "move-data"):
        assert command in out
    assert "usage:" not in out


class Updater:
    def __init__(self, *states, refuse=None):
        self.states, self.refuse, self.started = list(states), refuse, 0

    def start(self):
        from server.pi_update import UpdateError
        if self.refuse:
            raise UpdateError(self.refuse)
        self.started += 1

    def read(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"active": state["phase"] in radio345.UPDATE_PHASES, **state}


def run_update(monkeypatch, updater, code="old"):
    from server import devices
    monkeypatch.setattr(radio345.sys, "platform", "darwin")
    monkeypatch.setattr(radio345.time, "sleep", lambda s: None)
    monkeypatch.setattr(devices, "pi_host", lambda env: "pi@test")
    monkeypatch.setattr(devices, "code_hash", lambda: "same")
    monkeypatch.setattr(devices, "remote", lambda host: {"reachable": True, "code": code})
    return radio345.update_pi(updater)


def test_update_shows_phases_and_result(monkeypatch, capsys):
    updater = Updater({"phase": "copying"}, {"phase": "checking"}, {"phase": "restarting"}, {"phase": "done", "units": ["api"]})
    assert run_update(monkeypatch, updater) == 0
    out = capsys.readouterr().out
    assert all(text in out for text in radio345.UPDATE_PHASES.values()) and "install.sh --units" in out


def test_update_failure_and_refusal_are_explained(monkeypatch, capsys):
    assert run_update(monkeypatch, Updater({"phase": "failed", "error": "повернуто попередній код"})) == 1
    assert run_update(monkeypatch, Updater({"phase": "idle"}, refuse="спершу commit і push")) == 1
    out = capsys.readouterr().out
    assert "повернуто" in out and "commit і push" in out


def test_update_does_nothing_when_code_is_the_same(monkeypatch, capsys):
    updater = Updater({"phase": "idle"})
    assert run_update(monkeypatch, updater, code="same") == 0 and updater.started == 0


def test_update_is_refused_on_pi(monkeypatch, capsys):
    monkeypatch.setattr(radio345.sys, "platform", "linux")
    assert radio345.update() == 1 and "з Mac" in capsys.readouterr().out


def test_update_order_and_stop_on_failure(monkeypatch):
    monkeypatch.setattr(radio345.sys, "platform", "darwin")
    done, result = [], {"mac": 0}
    monkeypatch.setattr(radio345, "update_mac", lambda updater=None: done.append("mac") or result["mac"])
    monkeypatch.setattr(radio345, "update_pi", lambda updater=None: done.append("pi") or 0)
    assert radio345.update() == 0 and done == ["mac", "pi"]
    done.clear()
    result["mac"] = 1
    assert radio345.update() == 1 and done == ["mac"], "Pi не оновлюємо, якщо Mac не вдалося"
    done.clear()
    assert radio345.update("pi") == 0 and done == ["pi"]


def test_named_sets_expand_and_unknown_name_lists_them():
    profile = radio345.load_profile("mac-all")
    assert set(radio345.selected(profile, ["whisper"])) == {"worker", "panel"}
    assert "tunnel" in radio345.selected(profile, ["home-miniapp"]) and "tunnel" not in radio345.selected(profile, ["home"])
    for name in profile["sets"]:
        assert "panel" in radio345.selected(profile, [name])
    with pytest.raises(SystemExit, match="whisper"):
        radio345.selected(profile, ["невідомий"])


class FakeHome:
    def __init__(self, marker="mac", problems=(), phase="done"):
        self.marker, self.problems, self.phase, self.calls, self.moving = marker, list(problems), phase, [], False

    def read(self):   # водночас стан пристроїв і перенесення
        if self.moving:
            self.moving = False
            return {"phase": "copying", "active": True}
        return {"marker": self.marker, "home": None, "mode": "mac", "pi": None, "phase": self.phase, "active": False,
                "interrupted": False, "target": "pi", "error": "синтетичний збій"}

    def apply_mode(self, mode):
        self.calls.append(mode)

    def check(self, target):
        return {"problems": self.problems, "warnings": ["увага"]}

    def start(self, target, accept):
        self.calls.append(("move", target))
        self.moving = True


@pytest.mark.parametrize("target,answer,fake,code,calls", [
    (None, "", FakeHome(), 0, []),
    ("pi", "y", FakeHome(), 0, [("move", "pi")]),
    ("pi", "", FakeHome(), 1, []),
    ("pi", "y", FakeHome(problems=["ні"]), 1, []),
    ("pi", "y", FakeHome(phase="rolled_back"), 1, [("move", "pi")]),
    ("pi", "y", FakeHome(marker="pi"), 0, ["pi"]),
    ("off", "y", FakeHome(), 0, ["off"]),
    ("off", "", FakeHome(), 1, [])])
def test_home_command_moves_only_after_yes(monkeypatch, tmp_path, target, answer, fake, code, calls):
    monkeypatch.setattr(radio345, "ROOT", tmp_path)
    assert radio345.home(target, state=fake, mover=fake, ask=lambda text: answer, sleep=lambda s: None) == code
    assert fake.calls == calls


def test_pi_backups_run_without_mac_panel(monkeypatch):
    from types import SimpleNamespace
    made, lines = [], []
    daily = lambda root, host: (made.append(host), SimpleNamespace(start=lambda: "started"))[1]
    monkeypatch.setattr(radio345, "base_environment", lambda *a: {"RADIO345_PI_SSH": "u@pi"})
    assert radio345.start_pi_backups(lambda *a: lines.append(a), daily, "darwin") == "started" and made == ["u@pi"]
    assert radio345.start_pi_backups(lambda *a: lines.append(a), daily, "linux") is None and len(made) == 1

    def broken(root, host):
        raise RuntimeError
    assert radio345.start_pi_backups(lambda *a: lines.append(a), broken, "darwin") is None and lines


def test_doctor_reads_data_from_home(monkeypatch, capsys):
    up, seen = {"reachable": True, "processes": {}, "code": "same"}, []
    code, out = doctor(monkeypatch, capsys, up, home="pi", seen=seen)
    assert code == 0 and "База Pi" in out and seen[0][0] == "ssh"
    code, out = doctor(monkeypatch, capsys, up, seen=seen[:0])
    assert "База Pi" not in out
    code, out = doctor(monkeypatch, capsys, up, home="pi", rows="не список рядків")
    assert code == 1 and "Дані Pi" in out
