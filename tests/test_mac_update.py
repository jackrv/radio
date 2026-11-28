"""Оновлення Mac: справжні тимчасові репозиторії; лише вперед, з чистого дерева, з поверненням при збої."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from server import mac_update as u
from shared.busy import exclusive

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], env=ENV, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    origin, mac = tmp_path / "origin", tmp_path / "mac"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")

    def commit(name="a.py", text="1"):
        (origin / name).parent.mkdir(parents=True, exist_ok=True)
        (origin / name).write_text(text)
        (origin / ".gitignore").write_text(".local-data/\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "x")
        return git(origin, "rev-parse", "HEAD")
    commit()
    subprocess.run(["git", "clone", "-q", str(origin), str(mac)], env=ENV, check=True)
    python = []
    world = SimpleNamespace(origin=origin, mac=mac, commit=commit, python=python, fail=None, restarted=[])

    def run(command, **kwargs):
        if command[0] == sys.executable:
            python.append("pip" if "pip" in command else "import")
            return SimpleNamespace(returncode=int(world.fail == python[-1]), stdout="")
        return subprocess.run(command, **kwargs)
    control = SimpleNamespace(read=lambda: {"processes": {"asr": {"wanted": True}, "tunnel": {"wanted": False}}},
                              request=lambda verb, names: world.restarted.append((verb, names)))
    world.updater = u.MacUpdate(mac, run, control)
    return world


def finish(world):
    world.updater.start()
    world.updater.thread.join(30)
    return world.updater.state


def test_newer_commit_is_taken_and_only_wanted_services_restart(world):
    new = world.commit(text="2")
    assert world.updater.available() == new
    assert finish(world)["changed"] is True and git(world.mac, "rev-parse", "HEAD") == new
    assert world.python == ["import"] and world.restarted == [("restart", ["asr"])] and world.updater.available() is None


def test_same_code_changes_nothing(world):
    assert world.updater.available() is None
    assert finish(world) == {"phase": "done", "changed": False} and world.restarted == []


def test_changed_requirements_are_installed_and_supervisor_is_reported(world):
    world.commit("deploy/requirements/base.txt")
    world.commit("radio345.py")
    state = finish(world)
    assert world.python == ["pip", "import"] and state["supervisor"] is True


def test_worker_only_folder_checks_its_own_modules_and_requirements(tmp_path):
    # Служба Whisper (профіль mac-worker) — лише частина файлів і свої залежності.
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "asr_worker.py").write_text("")
    (tmp_path / "radio345.py").write_text("")
    (tmp_path / "deploy" / "profiles").mkdir(parents=True)
    (tmp_path / "deploy" / "requirements").mkdir()
    (tmp_path / "deploy" / "requirements" / "worker.txt").write_text("")
    (tmp_path / "deploy" / "profiles" / "mac-worker.toml").write_text('requirements = "deploy/requirements/worker.txt"\n')
    (tmp_path / ".env").write_text("RADIO345_PROFILE=mac-worker\n")
    updater = u.MacUpdate(tmp_path)
    assert updater.modules() == ["worker.asr_worker", "radio345"]
    assert updater.requirements() == "deploy/requirements/worker.txt"
    (tmp_path / ".env").write_text("")
    assert updater.requirements() == "deploy/requirements/dev.txt"


@pytest.mark.parametrize("fail", ["import", "pip"])
def test_broken_update_is_rolled_back(world, fail):
    before = git(world.mac, "rev-parse", "HEAD")
    world.commit("deploy/requirements/base.txt")
    world.fail = fail
    state = finish(world)
    assert state["phase"] == "failed" and "повернуто" in state["error"]
    assert git(world.mac, "rev-parse", "HEAD") == before and world.restarted == []


def test_local_work_is_never_overwritten(world):
    world.commit(text="2")
    (world.mac / "a.py").write_text("моя правка")
    with pytest.raises(u.UpdateError, match="незакомічені"):
        world.updater.start()
    assert (world.mac / "a.py").read_text() == "моя правка"
    git(world.mac, "checkout", "-q", "--", "a.py")
    (world.mac / "b.py").write_text("локальний коміт")
    git(world.mac, "add", "-A")
    git(world.mac, "commit", "-qm", "local")
    before = git(world.mac, "rev-parse", "HEAD")
    assert "розійшлися" in finish(world)["error"] and git(world.mac, "rev-parse", "HEAD") == before


def test_unpushed_local_commit_is_not_called_outdated(world):
    (world.mac / "b.py").write_text("x")
    git(world.mac, "add", "-A")
    git(world.mac, "commit", "-qm", "local")
    assert world.updater.available() is None and finish(world) == {"phase": "done", "changed": False}


def test_other_branch_and_busy_are_refused(world):
    with exclusive(world.mac):
        assert "Вже триває" in finish(world)["error"]
    git(world.mac, "checkout", "-q", "-b", "other")
    with pytest.raises(u.UpdateError, match="main"):
        world.updater.start()


def test_without_network_nothing_changes(world):
    git(world.mac, "remote", "set-url", "origin", str(world.mac / "none"))
    assert world.updater.available() is None and "GitHub" in finish(world)["error"]


def test_service_folder_takes_main_of_the_dev_folder_without_github(tmp_path):
    # Служба Whisper (~/radio345-mac) оновлюється з папки розробки, як Pi: потрібен лише коміт.
    dev, service = tmp_path / "dev", tmp_path / "service"
    dev.mkdir()
    git(dev, "init", "-q", "-b", "main")
    (dev / "a.py").write_text("1")
    git(dev, "add", "-A"); git(dev, "commit", "-qm", "x")
    subprocess.run(["git", "clone", "-q", str(dev), str(service)], env=ENV, check=True)
    git(service, "remote", "set-url", "origin", "https://example.invalid/repo.git")   # GitHub недоступний
    (dev / "a.py").write_text("2")
    git(dev, "commit", "-qam", "y")
    calls, restarted = [], []

    def run(command, **kwargs):
        if command[0] == "/service/python":
            calls.append(command[1:3])
            return SimpleNamespace(returncode=0, stdout="")
        return subprocess.run(command, **kwargs)
    control = SimpleNamespace(read=lambda: {"processes": {"worker": {"wanted": True}}},
                              request=lambda verb, names: restarted.append((verb, names)))
    updater = u.MacUpdate(service, run, control, source=str(dev), python="/service/python")
    updater.start(); updater.thread.join(30)
    assert updater.state["changed"] is True and git(service, "rev-parse", "HEAD") == git(dev, "rev-parse", "HEAD")
    assert calls == [["-c", "import server.radio_node,server.asr_service,server.api.local_dashboard,server.api.local_server,worker.asr_worker,radio345"]]
    assert restarted == [("restart", ["worker"])]
