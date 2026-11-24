"""Зв'язок ефіру з рештою через файли: вузол не відкриває базу.

``control.json`` пише служба розпізнавання (власник бази): пауза, прослуховування, VAD.
``state.json`` пише вузол: чи відкрито мікрофон. Немає файла чи він пошкоджений — ефір працює далі.
"""
import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

VAD_KEYS = {"VAD_LEVEL": (0, 3), "SILENCE_END_MS": (100, 5000), "MIN_SPEECH_MS": (50, 5000), "MAX_SEGMENT_S": (1, 60)}
MAX_HOLD_S = 130.0
STATES = ("listening", "paused", "stopped")


def directory(config):
    return Path(os.environ.get("RADIO_NODE_DIR", "").strip() or Path(config.BASE_DIR) / ".local-node")


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)   # 0600
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
    os.replace(temp, path)


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _deadline(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else 0.0


class NodeControl:
    """Бік вузла."""

    def __init__(self, folder, *, listening_hold=True, clock=time.time, monotonic=time.monotonic):
        self.path, self.state_path = Path(folder) / "control.json", Path(folder) / "state.json"
        self.listening_hold, self.clock, self.monotonic = listening_hold, clock, monotonic
        self._checked, self._data = float("-inf"), {}
        self._state, self._gap = "stopped", None

    def _load(self):
        if self.monotonic() - self._checked >= 0.5:
            self._checked = self.monotonic()
            data = read_json(self.path)
            if data is not None:
                self._data = data   # зник або пошкоджений — діє останнє відоме, строки самі закінчаться
        return self._data

    def request(self):
        """"pause" | "listening" | False — як reception_request, без бази."""
        data, now = self._load(), self.clock()
        if _deadline(data.get("pause_until")) > now:
            return "pause"
        hold = _deadline(data.get("hold_until"))
        # Утримання для луни мікрофона; лінійному входу Pi воно не потрібне (RADIO_LISTENING_HOLD=0).
        return "listening" if self.listening_hold and now < hold <= now + MAX_HOLD_S else False

    def apply_startup(self, target):
        """VAD з панелі → config, з перевіркою меж. Пристрої звуку — shared/audio_choice.py."""
        data = read_json(self.path) or {}
        for key, (low, high) in VAD_KEYS.items():
            value = data.get(key)
            if type(value) is int and low <= value <= high:
                setattr(target, key, value)

    # runtime_state для ApplicationSupervisor
    def set(self, state):
        if state not in STATES:
            raise ValueError("Неприпустимий стан вузла")
        self._state = state

    def get(self):
        return self._state

    def record_gap(self, seconds):
        """on_suspension: мікрофон був відкритий, а цикл зупинявся."""
        self._gap = {"seconds": float(seconds), "at": self.clock()}

    def publish(self):
        write_json(self.state_path, {"state": self._state, "at": self.clock(),
                                     **({"gap": self._gap} if self._gap else {})})

    @contextmanager
    def heartbeat(self, interval=2.0, log=None):
        stop = threading.Event()

        def run():
            while True:
                try:
                    self.publish()
                except OSError:
                    if log:
                        log.warning("Стан вузла не записано")
                if stop.wait(interval):
                    return

        thread = threading.Thread(target=run, name="node-state", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(5)
            self._state = "stopped"
            try:
                self.publish()
            except OSError:
                pass
