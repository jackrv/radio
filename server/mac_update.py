"""Оновлення коду цього Mac із GitHub: лише вперед (ff-only) і лише з чистого дерева. Збій перевірки повертає попередній коміт."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from shared.busy import Busy, exclusive

CHECK_S = 600.0
MODULES = "server.radio_node,server.asr_service,server.api.local_dashboard,server.api.local_server,worker.asr_worker,radio345"
SUPERVISOR = ("radio345.py", "shared/r345_control.py", "shared/process_guard.py", "shared/conflict.py")


class UpdateError(Exception):
    """Текст показується оператору."""


class MacUpdate:
    def __init__(self, root, run=subprocess.run, control=None, clock=time.monotonic):
        self.root, self.run, self.control, self.clock = Path(root), run, control, clock
        self.state, self.thread, self.guard = {"phase": "idle"}, None, threading.Lock()
        self._checked, self._available = float("-inf"), None

    def git(self, *args, timeout=60):
        try:
            done = self.run(["git", "-C", str(self.root), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def available(self):
        """Новий коміт main на GitHub, якого тут немає, або None. Мережа — не частіше разу на 10 хв."""
        if self.clock() - self._checked >= CHECK_S:
            self._checked = self.clock()
            out = self.git("ls-remote", "origin", "refs/heads/main", timeout=10)
            remote = out.split()[0] if out else None
            # Є в нашій історії — ми не позаду (могли бути попереду).
            known = remote is None or self.git("merge-base", "--is-ancestor", remote, "HEAD") is not None
            self._available = None if known else remote
        return self._available

    def read(self):
        return {**self.state, "available": self.available(), "active": self.thread is not None and self.thread.is_alive()}

    def precheck(self):
        if self.git("rev-parse", "--abbrev-ref", "HEAD") != "main":
            raise UpdateError("Mac не на гілці main")
        if self.git("status", "--porcelain", "--untracked-files=no") != "":
            raise UpdateError("На Mac є незакомічені зміни: спершу commit")

    def start(self):
        with self.guard:
            if self.thread is not None and self.thread.is_alive():
                raise UpdateError("Оновлення вже триває")
            self.precheck()
            self.state = {"phase": "copying"}
            self.thread = threading.Thread(target=self.update, name="mac-update", daemon=True)
            self.thread.start()

    def update(self):
        try:
            with exclusive(self.root):
                self._update()
        except Busy as exc:
            self.state = {"phase": "failed", "error": str(exc)}
        except UpdateError as exc:
            self.state = {"phase": "failed", "error": str(exc)}
        except Exception as exc:
            self.state = {"phase": "failed", "error": f"Несподівана помилка: {type(exc).__name__}"}
        self._checked = float("-inf")

    def _update(self):
        before = self.git("rev-parse", "HEAD")
        if self.git("fetch", "-q", "origin", "main", timeout=120) is None:
            raise UpdateError("GitHub недоступний: перевірте інтернет")
        target = self.git("rev-parse", "origin/main")
        if self.git("merge-base", "--is-ancestor", target, "HEAD") is not None:
            self.state = {"phase": "done", "changed": False}
            return
        if self.git("merge-base", "--is-ancestor", "HEAD", target) is None:
            raise UpdateError("Mac і GitHub розійшлися: об'єднайте вручну")
        changed = (self.git("diff", "--name-only", before, target) or "").splitlines()
        if self.git("merge", "-q", "--ff-only", target) is None:
            raise UpdateError("Не вдалося застосувати оновлення")
        self.state = {"phase": "checking"}
        try:
            if any(n.startswith("deploy/requirements/") for n in changed):
                self._python("-m", "pip", "install", "-q", "-r", "deploy/requirements/dev.txt", error="Залежності не встановилися")
            self._python("-c", "import " + MODULES, error="Новий код не завантажується")
        except UpdateError as exc:
            if self.git("reset", "-q", "--keep", before) is None:
                raise UpdateError(f"{exc}; повернути попередній код не вдалося") from None
            raise UpdateError(f"{exc}; повернуто попередній") from None
        self.state = {"phase": "restarting"}
        restart_all = any(n in SUPERVISOR for n in changed)
        if self.control is not None:
            running = self.control.read()
            if running:
                self.control.request("restart", [n for n, p in running["processes"].items() if p.get("wanted", True)])
        self.state = {"phase": "done", "changed": True, "supervisor": restart_all, "at": time.time()}

    def _python(self, *args, error):
        try:
            done = self.run([sys.executable, *args], cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError(error) from None
        if done.returncode:
            raise UpdateError(error)
