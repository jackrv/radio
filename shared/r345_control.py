"""Стан і керування запущеним ``r345``: файли .radio345-launcher/, без токенів і каналів."""
import fcntl
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from shared.instance_lock import lock_held
from shared.paths import place

ROOT = Path(__file__).resolve().parent.parent
VERBS = {"restart": "перезапуск", "stop": "зупинка", "start": "запуск"}


class Control:
    """Замок, стан і запити в .radio345-launcher/ (права 0700)."""

    def __init__(self, directory=None):
        self.directory = Path(directory or place(ROOT, ".radio345-launcher"))
        self.lock_path = self.directory / "lock"
        self.state_path = self.directory / "r345.json"
        self.lock_fd, self._last = None, None

    def acquire(self):
        if self.directory.is_symlink():
            raise SystemExit("Каталог керування не може бути символічним посиланням")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SystemExit("Radio 345 уже запущено (або ще завершується): ./r345 status") from None
        self.lock_fd = fd
        for verb in VERBS:
            for old in self.directory.glob(f"r345-{verb}-*"):
                old.unlink(missing_ok=True)

    def held(self):
        return lock_held(self.lock_path)

    def save(self, profile, children):
        data = {"pid": os.getpid(), "profile": profile, "processes": {c.name: c.state() for c in children}}
        if data == self._last:
            return
        fd, temp = tempfile.mkstemp(dir=self.directory)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
        os.replace(temp, self.state_path)
        self._last = data

    def read(self):
        """Стан живого r345 або None."""
        if not self.held():
            return None
        try:
            data = json.loads(self.state_path.read_text())
            pid = data["pid"]
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return data if type(pid) is int and "radio345 up" in command else None

    def request(self, verb, names):
        if verb not in VERBS:
            raise ValueError("Невідома дія")
        for name in names:
            for other in VERBS:   # остання команда скасовує попередню
                (self.directory / f"r345-{other}-{name}").unlink(missing_ok=True)
            fd = os.open(self.directory / f"r345-{verb}-{name}", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    def take_requests(self, children):
        for child in children:
            for verb, text in VERBS.items():
                path = self.directory / f"r345-{verb}-{child.name}"
                if path.exists():
                    path.unlink(missing_ok=True)
                    child.output(child.name, f"{text} за командою")
                    getattr(child, verb)()

    def release(self):
        self.state_path.unlink(missing_ok=True)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def request_stop(self):
        """True — сигнал наглядачу надіслано."""
        data = self.read()
        if data is None:
            return False
        os.kill(data["pid"], signal.SIGTERM)
        return True
