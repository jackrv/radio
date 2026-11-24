"""Агент перевстановлення iOS: знімок поза ~/Documents, без справжніх launchctl і Xcode."""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "tools" / "ios_reinstall.sh"
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="лише Mac")


@pytest.fixture
def env(tmp_path):
    root, home, bin_ = tmp_path / "repo", tmp_path / "home", tmp_path / "bin"
    (root / "tools").mkdir(parents=True)
    shutil.copy(SOURCE, root / "tools" / "ios_reinstall.sh")
    config = root / "apps" / "ios" / "Config"
    config.mkdir(parents=True)
    (config / "Signing.local.xcconfig").write_text("X = 1")
    (root / ".local-ios").mkdir()
    (root / ".local-ios" / "device-id").write_text("DEVICE-TEST\n")
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    bin_.mkdir()
    for name in ("launchctl", "xcodebuild", "xcrun", "osascript"):
        (bin_ / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/calls"\nexit ${{FAKE_{name}:-0}}\n')
        (bin_ / name).chmod(0o755)
    (bin_ / "security").write_text(f'#!/bin/sh\ncat "{tmp_path}/profile" 2>/dev/null\n')
    (bin_ / "security").chmod(0o755)
    values = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin:/usr/sbin:/sbin"}

    def run(*args, at=root, **extra):
        return subprocess.run(["/bin/bash", str(at / "tools" / "ios_reinstall.sh"), *args], env={**values, **extra},
                              capture_output=True, text=True)
    run.root, run.home, run.calls = root, home, tmp_path / "calls"
    run.profile = tmp_path / "profile"
    run.snapshot = home / "Library" / "Application Support" / "radio345" / "ios"
    run.agent = home / "Library" / "LaunchAgents" / "local.radio345.ios-reinstall.plist"
    return run


def test_agent_uses_private_snapshot(env):
    assert env("--install-agent").returncode == 0
    data = plistlib.loads(env.agent.read_bytes())
    assert data["ProgramArguments"][1:] == [str(env.snapshot / "tools/ios_reinstall.sh"), "--scheduled"]
    assert str(env.root) not in env.agent.read_text()
    assert (env.snapshot / "apps/ios/Config/Signing.local.xcconfig").exists()
    assert (env.snapshot / ".local-ios/device-id").read_text() == "DEVICE-TEST\n"
    assert os.stat(env.snapshot).st_mode & 0o777 == 0o700
    assert "увімкнено" in env("--status").stdout


def test_snapshot_builds_without_repository(env):
    env("--install-agent")
    shutil.rmtree(env.root)
    done = env("--force", at=env.snapshot)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (env.snapshot / ".local-ios/last-install").exists()
    assert str(env.snapshot / "apps/ios") in env.calls.read_text()


def test_manual_install_refreshes_snapshot_and_skips_next_run(env):
    env("--install-agent")
    (env.root / "apps/ios/new.swift").write_text("//")
    assert env("--force").returncode == 0
    assert (env.snapshot / "apps/ios/new.swift").exists()
    env.calls.unlink()
    assert "пропускаю" in env(at=env.snapshot).stdout and not env.calls.exists()


def test_failed_install_keeps_snapshot_and_mark(env):
    env("--install-agent")
    (env.root / "apps/ios/new.swift").write_text("//")
    assert env("--force", FAKE_xcrun="1").returncode == 1
    assert not (env.snapshot / "apps/ios/new.swift").exists()
    assert not (env.root / ".local-ios/last-install").exists()


def test_build_only_never_touches_phone(env):
    assert env("--build").returncode == 0
    assert "xcrun" not in env.calls.read_text() and not (env.root / ".local-ios/last-install").exists()


def test_install_agent_refuses_without_signing(env):
    (env.root / "apps/ios/Config/Signing.local.xcconfig").unlink()
    assert env("--install-agent").returncode == 1 and not env.agent.exists()


def test_old_agent_is_reported(env):
    env.agent.write_text("/somewhere/Documents/tools/ios_reinstall.sh")
    assert "старий" in env("--status").stdout


def profile(env, left_h):
    """Профіль, що діє ще left_h годин."""
    from datetime import datetime, timedelta, timezone
    env.profile.write_bytes(plistlib.dumps({"ExpirationDate": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=left_h)}))


def scheduled(env, **extra):
    return env("--scheduled", at=env.snapshot, **{"HOUR": "0", **extra})


def builds(env):
    return env.calls.read_text().count("xcodebuild") if env.calls.exists() else 0


def installs(env):
    return env.calls.read_text().count("install app") if env.calls.exists() else 0


@pytest.fixture
def agent(env):
    profile(env, 150)
    env("--install-agent")
    assert env("--force").returncode == 0
    env.calls.unlink()
    env.mark = env.snapshot / ".local-ios" / "last-install"
    return env


def test_valid_signature_is_left_alone(agent):
    assert scheduled(agent).returncode == 0 and builds(agent) == 0


def test_same_expiry_is_not_reinstalled_but_tried_next_day(agent):
    profile(agent, 20)
    import time
    expiry = plistlib.loads(agent.profile.read_bytes())["ExpirationDate"]
    agent.mark.write_text(str(int(expiry.replace(tzinfo=__import__("datetime").timezone.utc).timestamp())))
    assert "не чіпаю" in scheduled(agent).stdout and builds(agent) == 1 and installs(agent) == 0
    scheduled(agent)
    assert builds(agent) == 1   # сьогодні вже пробували
    (agent.snapshot / ".local-ios" / "last-try").write_text("2026-01-01")
    profile(agent, 168)   # Xcode продовжив
    assert scheduled(agent).returncode == 0 and installs(agent) == 1
    assert int(agent.mark.read_text()) > time.time() + 160 * 3600


def test_expired_signature_is_retried_every_run(agent):
    agent.mark.write_text("1000")
    for _ in range(2):
        assert scheduled(agent, FAKE_xcrun="1").returncode == 1
    assert agent.calls.read_text().count("osascript") == 1   # одне сповіщення на добу
    assert builds(agent) == 2 and agent.mark.read_text() == "1000"
    assert scheduled(agent).returncode == 0 and installs(agent) == 3


def test_night_is_skipped_while_signature_valid(agent):
    import time
    agent.mark.write_text(str(int(time.time()) + 3600))
    assert scheduled(agent, HOUR="24").returncode == 0 and builds(agent) == 0


def test_old_empty_mark_uses_age(agent):
    agent.mark.write_text("")
    assert scheduled(agent).returncode == 0 and builds(agent) == 0
    old = agent.mark.stat().st_mtime - 6 * 86400
    os.utime(agent.mark, (old, old))
    assert scheduled(agent).returncode == 0 and installs(agent) == 1
