"""Защита от обработки сегментов предыдущего сеанса приёма."""

import threading


class PauseGate:
    def __init__(self):
        self._lock = threading.RLock()
        self._paused = False
        self._generation = 0

    def set_paused(self, paused):
        with self._lock:
            paused = bool(paused)
            if paused != self._paused:
                self._generation += 1
                self._paused = paused

    def tag(self):
        with self._lock:
            return None if self._paused else self._generation

    def is_current(self, tag):
        with self._lock:
            return (
                not self._paused
                and tag is not None
                and tag == self._generation
            )

    def run_if_current(self, tag, callback):
        with self._lock:
            if self._paused or tag is None:
                return False
            if tag != self._generation:
                return False

            callback()
            return True
