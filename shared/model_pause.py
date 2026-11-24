"""Тимчасово віддати модель вузла Whisper лабораторії: файлова оренда без бази.

Запит — ``.radio345-launcher/model-pause.json`` (до якого часу); відповідь вузла — model-state.json.
Строк обов'язковий: якщо лабораторія впала й не повернула модель, вузол забере її сам.
"""
import json
import math
import os
import tempfile
import time
from pathlib import Path
from shared.paths import place

MAX_PAUSE_S = 4 * 60 * 60
WORKER_LOCK = ".radio345-worker.lock"   # вузол тримає весь час життя, навіть коли модель віддано


def model_root(default, env=None):
    """Каталог замків моделі: інший checkout вказують через RADIO_ASR_LOCK_ROOT."""
    return Path((os.environ if env is None else env).get("RADIO_ASR_LOCK_ROOT", "").strip() or default)


class ModelPause:
    """Оренда моделі для черги лабораторії: start/renew/ready/stop/restored. Вузла немає — модель і так вільна."""

    def __init__(self, root):
        self.root = Path(root)
        self.directory = place(self.root, ".radio345-launcher")
        self.request_path = self.directory / "model-pause.json"
        self.state_path = self.directory / "model-state.json"

    def _write(self, path, data):
        if self.directory.is_symlink():
            raise RuntimeError("Каталог керування не може бути символічним посиланням")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.directory)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
        os.replace(temp, path)

    @staticmethod
    def _read(path):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- з боку лабораторії й r345 ----------
    def worker_running(self):
        from shared.instance_lock import lock_held
        return lock_held(place(self.root, WORKER_LOCK))

    def start(self, until):
        if not time.time() < until <= time.time() + MAX_PAUSE_S:
            raise ValueError(f"Паузу моделі задано поза межами {MAX_PAUSE_S} с")
        self._write(self.request_path, {"until": float(until)})

    renew = start

    def stop(self):
        self.request_path.unlink(missing_ok=True)

    def state(self):
        return self._read(self.state_path).get("state") if self.worker_running() else None

    def ready(self):
        return self.requested() and (not self.worker_running() or self.state() == "paused")

    def restored(self):
        return not self.requested() and self.state() in (None, "ready")

    # ---------- з боку вузла ----------
    def requested(self):
        until = self._read(self.request_path).get("until")
        # Пошкоджений або задалекий строк не має відібрати модель назавжди.
        return type(until) in (int, float) and math.isfinite(until) and time.time() < until <= time.time() + MAX_PAUSE_S

    def report(self, state):
        self._write(self.state_path, {"state": state, "pid": os.getpid(), "at": time.time()})
