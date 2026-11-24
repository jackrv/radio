"""Служба macOS: не з захищених тек, без повторів після штатної зупинки, не поруч з іншим r345."""
import plistlib
from types import SimpleNamespace

import pytest

import radio345
from shared import mac_service as m


@pytest.fixture
def world(tmp_path):
    home, root = tmp_path / "home", tmp_path / "home" / "radio345"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").touch()
    calls = []

    def run(command, **kwargs):
        calls.append(command[1])
        return SimpleNamespace(returncode=world.code, stdout="", stderr="")
    world = SimpleNamespace(home=home, root=root, calls=calls, run=run, code=0)
    return world


@pytest.mark.parametrize("folder,where", [("Documents/x/radio", "~/Documents"), ("Desktop/r", "~/Desktop"), ("Downloads/r", "~/Downloads"),
                                          ("Library/Mobile Documents/r", "~/Library/Mobile Documents"), ("radio345", None), ("Documents2/r", None)])
def test_protected_folders(tmp_path, folder, where):
    root = tmp_path / folder
    root.mkdir(parents=True)
    assert m.protected(root, tmp_path) == where


def test_link_into_protected_folder_is_seen(tmp_path):
    (tmp_path / "Documents/radio").mkdir(parents=True)
    (tmp_path / "radio345").symlink_to(tmp_path / "Documents/radio")
    assert m.protected(tmp_path / "radio345", tmp_path) == "~/Documents"


def test_on_writes_service_and_off_removes_it(world):
    m.on(world.root, False, home=world.home, run=world.run)
    data = plistlib.loads(m.plist_path(home=world.home).read_bytes())
    assert data["ProgramArguments"][-2:] == ["radio345", "up"] and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["RADIO345_SERVICE"] == "1" and "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert str(world.root.resolve()) in data["StandardOutPath"] and world.calls == ["bootout", "bootstrap"]
    assert m.enabled(home=world.home) and m.off(home=world.home, run=world.run) and not m.enabled(home=world.home)
    assert m.off(home=world.home, run=world.run) is False


def test_refusals_change_nothing(world, tmp_path):
    with pytest.raises(m.Refused, match="stop"):
        m.on(world.root, True, home=world.home, run=world.run)
    documents = world.home / "Documents" / "radio"
    documents.mkdir(parents=True)
    with pytest.raises(m.Refused, match="Documents"):
        m.on(documents, False, home=world.home, run=world.run)
    with pytest.raises(m.Refused, match=".venv"):
        m.on(tmp_path, False, home=world.home, run=world.run)
    assert world.calls == [] and not m.enabled(home=world.home)


def test_rejected_by_launchd_leaves_no_file(world):
    world.code = 5
    with pytest.raises(m.Refused, match="launchd"):
        m.on(world.root, False, home=world.home, run=world.run)
    assert not m.enabled(home=world.home)


def test_log_is_bounded(world):
    log = m.log_path(world.root)
    log.parent.mkdir()
    log.write_bytes(b"x" * 10)
    m.trim_log(world.root)
    assert log.stat().st_size == 10
    with open(log, "wb") as out:
        out.truncate(m.LOG_LIMIT + 1)
    m.trim_log(world.root)
    assert log.stat().st_size == 0


def test_pi_is_sent_to_systemd(monkeypatch, capsys):
    monkeypatch.setattr(radio345.sys, "platform", "linux")
    assert radio345.service("on") == 1 and "systemd" in capsys.readouterr().out
