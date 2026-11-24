"""Рівень входу для схеми панелі: RMS за останню секунду й RMS останньої фрази — те саме число, що порівнює MIN_RMS."""
import collections
import threading
import time

import numpy as np

WINDOW_S = 1.0
STALE_S = 2.0   # мікрофон закрито — рівня немає


class LevelMeter:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.chunks = collections.deque()   # (час, сума квадратів, відліків, пік)
        self.last_segment = None            # (час, rms)

    def add(self, pcm):
        """Кадр PCM16 з потоку захоплення: дешево й без винятків."""
        try:
            x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            if not x.size:
                return
            now = self.clock()
            item = (now, float(np.dot(x, x)), x.size, float(np.abs(x).max()))
            with self.lock:
                self.chunks.append(item)
                while now - self.chunks[0][0] > WINDOW_S:
                    self.chunks.popleft()
        except Exception:
            pass

    def segment(self, pcm):
        try:
            x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            if x.size:
                with self.lock:
                    self.last_segment = (self.clock(), float(np.sqrt(np.mean(x ** 2))))   # як shared.audio.prepare_audio
        except Exception:
            pass

    def read(self):
        now = self.clock()
        with self.lock:
            chunks, segment = list(self.chunks), self.last_segment
        out = {"rms": None, "peak": None, "phrase_rms": None, "phrase_age_s": None}
        if chunks and now - chunks[-1][0] <= STALE_S:
            out["rms"] = round((sum(c[1] for c in chunks) / sum(c[2] for c in chunks)) ** 0.5, 4)
            out["peak"] = round(max(c[3] for c in chunks), 4)
        if segment:
            out["phrase_rms"], out["phrase_age_s"] = round(segment[1], 4), round(now - segment[0], 1)
        return out
