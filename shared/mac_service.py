"""Служба macOS (launchd): тримає ``./r345 up`` без вікна термінала. Лише поза захищеними теками.

Фонові процеси macOS не бачать ~/Documents, ~/Desktop і ~/Downloads, тому звідти служба не вмикається.
Штатна зупинка (код 0) — без повтору; падіння — повтор не частіше 30 с. Другий r345 не запускається.
"""
import os
import plistlib
import subprocess
from pathlib import Path

from shared.paths import DATA

LABEL = "local.radio345.service"
PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")
LOG_LIMIT = 5 * 1024 * 1024


class Refused(Exception):
    """Текст показується оператору."""


def protected(root, home=None):
    """Захищена тека, у якій лежить проєкт, або None."""
    root, home = Path(root).resolve(), Path(home or Path.home()).resolve()
    for name in PROTECTED:
        if root.is_relative_to(home / name):
            return f"~/{name}"
    return None


def plist_path(label=LABEL, home=None):
    return Path(home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def log_path(root):
    return Path(root) / DATA / "service.log"


def plist(root, label=LABEL):
    root = Path(root).resolve()
    return {"Label": label, "ProgramArguments": [str(root / ".venv" / "bin" / "python"), "-u", "-m", "radio345", "up"],
            "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "ExitTimeOut": 60, "ProcessType": "Interactive", "Umask": 0o077,
            "StandardOutPath": str(log_path(root)), "StandardErrorPath": str(log_path(root)),
            "EnvironmentVariables": {"RADIO345_SERVICE": "1", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}


def launchctl(*args, run=subprocess.run, check=False):
    return run(["/bin/launchctl", *args], check=check, capture_output=True, text=True, timeout=90)


def enabled(label=LABEL, home=None):
    return plist_path(label, home).exists()


def on(root, held, label=LABEL, home=None, run=subprocess.run):
    """held — чи вже працює r345 (з термінала): спершу його зупиняють."""
    where = protected(root, home)
    if where:
        raise Refused(f"Проєкт лежить у {where}: macOS не пускає туди фонові служби. Перенесіть його, наприклад у ~/radio345, "
                      "і створіть .venv заново")
    if not (Path(root) / ".venv" / "bin" / "python").exists():
        raise Refused("Немає .venv")
    if held:
        raise Refused("r345 уже працює: спершу ./r345 stop")
    (Path(root) / DATA).mkdir(mode=0o700, exist_ok=True)
    path = plist_path(label, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist(root, label)))
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{label}", run=run)
    if launchctl("bootstrap", domain, str(path), run=run).returncode:
        path.unlink(missing_ok=True)
        raise Refused("launchd не прийняв службу")


def off(label=LABEL, home=None, run=subprocess.run):
    """Зупинити й прибрати; True — була."""
    was = enabled(label, home)
    launchctl("bootout", f"gui/{os.getuid()}/{label}", run=run)   # SIGTERM: r345 зупиняє служби штатно
    plist_path(label, home).unlink(missing_ok=True)
    return was


def trim_log(root):
    """На старті служби: журнал не росте без меж (launchd дописує в кінець)."""
    path = log_path(root)
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.truncate(path, 0)
    except OSError:
        pass
