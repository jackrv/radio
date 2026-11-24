"""Оновлення коду Pi: лише файли профілю, збій повертає попередній код, чужі файли не чіпає."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import devices, pi_update as u


@pytest.fixture
def pi(tmp_path):
    root = tmp_path / u.FOLDER
    for path, text in (("deploy/profiles/pi.files", "# x\n/deploy/profiles/pi.files\n/server/a.py\n/server/old.py\n/deploy/requirements/hub.txt\n"),
                       ("server/a.py", "old"), ("server/old.py", "old"), ("deploy/requirements/hub.txt", "x"), (".env", "SECRET=1"),
                       ("events.db", "db"), ("notes.txt", "чуже")):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)

    def python(code):
        (venv / "python").write_text(f"#!/bin/sh\nexit {code}\n")
        (venv / "python").chmod(0o755)
    python(0)
    (venv / "pip").write_text("#!/bin/sh\ntouch pip-called\n")
    (venv / "pip").chmod(0o755)

    def step(name, *args):
        return subprocess.run([sys.executable, "-I", "-", u.FOLDER, name, *(args or (("c" * 40,) if name == "finish" else ()))], input=u.REMOTE, capture_output=True, text=True,
                              env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "USER": "pi"})

    def deliver():
        (root / "deploy/profiles/pi.files").write_text("/deploy/profiles/pi.files\n/server/a.py\n/server/new.py\n/deploy/requirements/hub.txt\n")
        (root / "server/a.py").write_text("new")
        (root / "server/new.py").write_text("new")
    return SimpleNamespace(root=root, step=step, deliver=deliver, python=python)


def test_update_replaces_code_and_removes_only_dropped_files(pi):
    assert pi.step("prepare").returncode == 0
    pi.deliver()
    done = pi.step("finish")
    assert done.returncode == 0 and json.loads(done.stdout)["units"] == [], done.stderr
    assert (pi.root / "server/a.py").read_text() == "new" and not (pi.root / "server/old.py").exists()
    assert (pi.root / ".local-data/commit").read_text() == "c" * 40
    for name in (".env", "events.db", "notes.txt"):
        assert (pi.root / name).exists()
    assert not (pi.root / "pip-called").exists(), "залежності не змінилися"


def test_broken_code_is_rolled_back(pi):
    pi.step("prepare")
    pi.deliver()
    pi.python(1)
    done = pi.step("finish")
    assert done.returncode and done.stderr.strip().splitlines()[-1] == "import"
    assert (pi.root / "server/a.py").read_text() == "old" and (pi.root / "server/old.py").exists()
    assert not (pi.root / "server/new.py").exists() and (pi.root / ".env").read_text() == "SECRET=1"


def test_changed_requirements_are_installed(pi):
    pi.step("prepare")
    pi.deliver()
    (pi.root / "deploy/requirements/hub.txt").write_text("y")
    assert pi.step("finish").returncode == 0 and (pi.root / "pip-called").exists()


def test_manifest_cannot_reach_private_or_outside_files(pi):
    outside = pi.root.parent / "outside.txt"
    outside.write_text("x")
    (pi.root / "deploy/profiles/pi.files").write_text("/deploy/profiles/pi.files\n/.env\n/../outside.txt\n/.local-recordings/a\n/server/a.py\n/deploy/requirements/hub.txt\n")
    pi.step("prepare")
    pi.deliver()
    pi.step("finish")
    assert outside.exists() and (pi.root / ".env").exists()


@pytest.fixture
def mac(tmp_path):
    """Репозиторій Mac: файли профілю й те, чого на Pi бути не має."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")

    def commit(manifest, **files):
        (repo / "deploy/profiles").mkdir(parents=True, exist_ok=True)
        (repo / "server").mkdir(exist_ok=True)
        (repo / "deploy/profiles/pi.files").write_text("\n".join(manifest) + "\n")
        (repo / "deploy/requirements").mkdir(parents=True, exist_ok=True)
        (repo / "deploy/requirements/hub.txt").write_text("x")
        (repo / "private-lab.txt").write_text("не для Pi")
        for name, text in files.items():
            (repo / f"server/{name}.py").write_text(text)
        git("add", "-A")
        git("commit", "-qm", "x")
        return git("rev-parse", "HEAD")
    return SimpleNamespace(repo=repo, commit=commit)


BASE = ["/deploy/profiles/pi.files", "/server/a.py", "/deploy/requirements/hub.txt"]


def deliver(pi, data, checksum=None):
    (pi.root / ".local-data").mkdir(exist_ok=True)
    (pi.root / ".local-data/code-update.tar.gz").write_bytes(data)
    return pi.step("unpack", checksum or hashlib.sha256(data).hexdigest())


def test_exact_commit_is_taken_not_the_newest(pi, mac):
    wanted = mac.commit([*BASE, "/server/new.py"], a="wanted", new="n")
    mac.commit([*BASE, "/server/new.py"], a="newer", new="n")
    pi.step("prepare")
    (pi.root / ".local-data/code.git").mkdir()
    assert deliver(pi, u.PiUpdate(mac.repo, "pi@test").archive(wanted)).returncode == 0
    assert pi.step("finish", wanted).returncode == 0
    assert (pi.root / "server/a.py").read_text() == "wanted" and (pi.root / "server/new.py").exists()
    assert not (pi.root / "private-lab.txt").exists() and not (pi.root / ".git").exists()
    assert not (pi.root / ".local-data/code-update.tar.gz").exists() and not (pi.root / ".local-data/code.git").exists()
    assert (pi.root / ".local-data/commit").read_text() == wanted and (pi.root / ".env").read_text() == "SECRET=1"


@pytest.mark.parametrize("damage", [lambda d: d[:-20], lambda d: b"x" + d[1:], lambda d: b""])
def test_damaged_transfer_changes_nothing(pi, mac, damage):
    data = u.PiUpdate(mac.repo, "pi@test").archive(mac.commit(BASE, a="new"))
    pi.step("prepare")
    done = deliver(pi, damage(data), hashlib.sha256(data).hexdigest())
    assert done.stderr.strip() == "checksum" and (pi.root / "server/a.py").read_text() == "old"
    assert deliver(pi, b"not a tar").stderr.strip() == "checksum"


def test_missing_file_is_refused_on_mac(mac):
    with pytest.raises(u.UpdateError, match="список"):
        u.PiUpdate(mac.repo, "pi@test").archive(mac.commit([*BASE, "/server/missing.py"], a="new"))


@pytest.mark.parametrize("listed,packed", [(["/.env"], [".env"]), (["/../outside.txt"], ["../outside.txt"]), (["/server/missing.py"], []), ([], ["server/extra.py"])])
def test_bad_archive_changes_nothing(pi, listed, packed):
    import io
    import tarfile
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name in [*BASE, *packed]:
            body = "\n".join([*BASE, *listed]).encode() if name.endswith("pi.files") else b"new"
            info = tarfile.TarInfo(name.lstrip("/"))
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    pi.step("prepare")
    assert deliver(pi, out.getvalue()).stderr.strip().splitlines()[-1] == "manifest"
    assert (pi.root / "server/a.py").read_text() == "old" and (pi.root / ".env").read_text() == "SECRET=1"
    assert not (pi.root.parent / "outside.txt").exists()


def updater(tmp_path, results, alive=("panel",), after=None):
    calls, restarted = [], []

    def run(command, **kwargs):
        name = "send" if "cat >" in command[-1] else command[-1].split(u.FOLDER)[1].split()[0]
        calls.append(name)
        code, out, err = results.get(name, (0, '{"ok": true}', ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)
    states = [{"processes": {n: {"alive": True} for n in alive}}, after or {"processes": {n: {"alive": True} for n in alive}}]
    fetch = lambda host: states.pop(0) if len(states) > 1 else states[0]
    update = u.PiUpdate(tmp_path, "pi@test", run, lambda h, p, v: restarted.append((p, v)), fetch, lambda s: None)
    update.archive = lambda commit: b"code"
    return update, calls, restarted


ARGS = ("a" * 40,)


def test_running_services_only_are_restarted(tmp_path):
    update, calls, restarted = updater(tmp_path, {}, alive=("panel", "asr"))
    update.update(*ARGS)
    assert update.state["phase"] == "done" and calls == ["prepare", "send", "unpack", "finish"]
    assert sorted(restarted) == [("asr", "restart"), ("panel", "restart")]


@pytest.mark.parametrize("step,result,text", [
    ("send", (1, "", ""), "не передано"), ("unpack", (1, "", "checksum\n"), "нічого не змінено"), ("finish", (1, "", "import\n"), "повернуто"), ("finish", (255, "", ""), "SSH"),
    ("finish", (0, "x", ""), "Незрозуміла")])
def test_failures_are_explained(tmp_path, step, result, text):
    update, _, restarted = updater(tmp_path, {step: result})
    update.update(*ARGS)
    assert update.state["phase"] == "failed" and text in update.state["error"] and restarted == []


def test_service_that_did_not_return_is_reported(tmp_path):
    update, _, _ = updater(tmp_path, {}, after={"processes": {"panel": {"alive": False}}})
    update.update(*ARGS)
    assert update.state["phase"] == "failed" and "panel" in update.state["error"]


def source(tmp_path, head="a" * 40, status="", ahead="0"):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout={"rev-parse": head, "status": status, "rev-list": ahead}[command[3]])
    return u.PiUpdate(tmp_path, "pi@test", run)


def test_only_clean_commit_is_sent_push_is_not_required(tmp_path):
    update = source(tmp_path, ahead="3")
    assert update.source() == "a" * 40 and update.unpushed == 3
    for kwargs, text in (({"status": " M a.py"}, "незакомічені"), ({"head": "main"}, "визначити")):
        with pytest.raises(u.UpdateError, match=text):
            source(tmp_path, **kwargs).source()


def test_without_host_is_refused(tmp_path):
    with pytest.raises(u.UpdateError):
        u.PiUpdate(tmp_path, None).start()


def test_update_and_move_exclude_each_other(tmp_path):
    from shared.busy import exclusive
    update, calls, _ = updater(tmp_path, {})
    with exclusive(tmp_path):
        update.update(*ARGS)
    assert update.state["phase"] == "failed" and "Вже триває" in update.state["error"] and calls == []
    update.update(*ARGS)
    assert update.state["phase"] == "done"
