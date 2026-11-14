"""Launcher checks use temporary files and mocked services, never the live radio."""

import signal
import subprocess
from unittest.mock import Mock

import pytest

import start_radio345 as launcher


@pytest.mark.parametrize("url", [
    "http://example.com", "https://user:secret@example.com",
    "https://example.com:443", "https://example.com/path",
    "https://example.com?secret=value", "https://example.com/#fragment",
    "https://a..com", "https://-domain.com", "https://example.com/\"",
])
def test_endpoint_validation_does_not_echo_input(url):
    with pytest.raises(ValueError) as error:
        launcher.valid_https_endpoint(url)
    assert url not in str(error.value)


def test_update_html_changes_only_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "miniapp").mkdir()
    page = tmp_path / "miniapp/index.html"
    original = '<meta name="radio345-api" content="https://old.example">\n<script src="correction-editor.js?v=4"></script>\n'
    page.write_text(original)
    assert launcher.update_miniapp_api_url("https://NEW.example/") is True
    assert page.read_text() == original.replace("https://old.example", "https://new.example")
    assert launcher.update_miniapp_api_url("https://new.example") is False


def test_update_html_requires_one_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "miniapp").mkdir()
    page = tmp_path / "miniapp/index.html"
    page.write_text("unchanged")
    with pytest.raises(RuntimeError):
        launcher.update_miniapp_api_url("https://new.example")
    assert page.read_text() == "unchanged"


@pytest.fixture
def mocked_services(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "api").mkdir()
    for name in ["radio.py", "api/local_server.py", ".env", "events.db"]:
        (tmp_path / name).touch()
    monkeypatch.setattr(launcher.sys, "argv", ["start_radio345.py"])
    monkeypatch.setattr(launcher, "launcher_setting", lambda *args: "")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/mock/ngrok")
    monkeypatch.setattr(launcher, "port_busy", lambda _: False)
    monkeypatch.setattr(launcher, "wait_for_api", Mock())
    monkeypatch.setattr(launcher, "wait_for_ngrok", lambda _: "https://radio.example")
    monkeypatch.setattr(launcher, "read_expected_api_url", lambda: "https://radio.example")
    monkeypatch.setattr(launcher, "update_miniapp_api_url", Mock())
    publish = Mock()
    monkeypatch.setattr(launcher, "publish_miniapp_url", publish)
    created = []
    stopped = []

    def process(name, command):
        item = Mock(name=name)
        item.name = name
        item.process.poll.return_value = None
        item.stop.side_effect = lambda **kw: stopped.append(name)
        created.append(item)
        return item

    monkeypatch.setattr(launcher, "ManagedProcess", process)
    return created, stopped, publish


def test_ctrl_c_stops_services_in_reverse_order_without_publishing(mocked_services, monkeypatch):
    created, stopped, publish = mocked_services
    monkeypatch.setattr(launcher.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    previous = signal.getsignal(signal.SIGINT)
    assert launcher.main() == 0
    assert [item.name for item in created] == ["api", "ngrok", "radio"]
    assert stopped == ["radio", "ngrok", "api"]
    assert signal.getsignal(signal.SIGINT) is previous
    publish.assert_not_called()


def test_failed_tunnel_stops_api_and_does_not_start_radio(mocked_services, monkeypatch):
    created, stopped, publish = mocked_services
    monkeypatch.setattr(launcher, "wait_for_ngrok", lambda _: None)
    assert launcher.main() == 1
    assert [item.name for item in created] == ["api", "ngrok"]
    assert stopped == ["ngrok", "api"]
    publish.assert_not_called()


def test_busy_port_starts_nothing(mocked_services, monkeypatch):
    created, stopped, _ = mocked_services
    monkeypatch.setattr(launcher, "port_busy", lambda _: True)
    assert launcher.main() == 2
    assert created == stopped == []


def test_stop_continues_if_one_service_cannot_stop(mocked_services, monkeypatch):
    created, stopped, _ = mocked_services

    def interrupt(_):
        created[-1].stop.side_effect = OSError("test")
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.time, "sleep", interrupt)
    assert launcher.main() == 0
    assert stopped == ["ngrok", "api"]


def test_process_stop_escalates_only_its_own_group(monkeypatch):
    child = launcher.ManagedProcess.__new__(launcher.ManagedProcess)
    child.name = "radio"
    child.process = Mock(pid=12345)
    child.process.poll.return_value = None
    child.process.wait.side_effect = [subprocess.TimeoutExpired("radio", 18), 0]
    kill = Mock()
    monkeypatch.setattr(launcher.os, "killpg", kill)
    child.stop(graceful_seconds=18)
    assert kill.call_args_list == [((12345, signal.SIGINT),), ((12345, signal.SIGKILL),)]


def test_publish_refuses_unrelated_staged_changes(monkeypatch):
    commands = []

    def git(*args, **kwargs):
        commands.append(args)
        return {
            ("rev-parse", "--show-toplevel"): str(launcher.ROOT),
            ("branch", "--show-current"): "main",
            ("diff", "--cached", "--name-only"): "radio.py",
        }[args]

    monkeypatch.setattr(launcher, "git", git)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/mock/git")
    with pytest.raises(RuntimeError, match="staged"):
        launcher.check_pages_publish_ready()
    assert not any(command[0] in ("commit", "push", "fetch") for command in commands)
