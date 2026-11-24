"""Поріг гучності для «Наживо»: фраза йде на iPhone лише відтоді, як рівень за WINDOW_S досяг LIVE_MIN_RMS.

Лише прослуховування: архів, розпізнавання й Telegram отримують усе, як раніше. 0 — вимкнено, без затримки.
"""
import numpy as np

from shared.diagnostics import runtime

WINDOW_S = 0.3   # за стільки міряємо рівень
HOLD_S = 0.8     # стільки звуку перед відкриттям не втрачається (preroll 0,5 с + вікно)


class LiveGate:
    def __init__(self, emit, threshold, sample_rate):
        self.emit, self.threshold = emit, threshold
        self.window, self.hold = int(WINDOW_S * sample_rate) * 2, int(HOLD_S * sample_rate) * 2
        self.open, self.held = False, b""

    def __call__(self, pcm, phase):
        if phase == "start":
            self.open, self.held = False, b""
        if not self.open:
            self._closed(pcm, phase)
        else:
            self.emit(pcm, phase)
        if phase == "end":
            self.open, self.held = False, b""

    def _closed(self, pcm, phase):
        limit = self.threshold()
        before = self.held
        self.held = before + pcm
        if limit > 0 and self._rms(self.held[-self.window:]) < limit:
            self.held = self.held[-self.hold:]
            if phase == "end":
                runtime.count("radio_stream.live_quiet")
            return
        self.open = True
        if phase == "end":
            self.emit(before, "start")
            self.emit(pcm, "end")
        else:
            self.emit(self.held, "start")
        self.held = b""

    @staticmethod
    def _rms(pcm):
        x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
