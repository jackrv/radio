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
