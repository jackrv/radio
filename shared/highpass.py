"""Зріз гулу мережі (50 Гц і гармоніки 100/150) до VAD, ефіру й архіву.

FIR з вікном Хеммінга, лише numpy (scipy на Pi немає): зріз CUTOFF_HZ, 1023 відводи —
50/100/150 Гц глушаться на ~50 дБ, мова рації (від ~300 Гц) не зачіпається;
затримка ~32 мс. Вмикається галочкою в панелі (shared/audio_choice.py), діє з перезапуску ефіру.
"""
import numpy as np

CUTOFF_HZ = 200
TAPS = 1023


def design(sample_rate, cutoff=CUTOFF_HZ, taps=TAPS):
    n = np.arange(taps) - (taps - 1) / 2
    low = np.sinc(2 * cutoff / sample_rate * n) * np.hamming(taps)
    low /= low.sum()
    high = -low
    high[(taps - 1) // 2] += 1.0
    return high


class HighPass:
    """Потоковий фільтр int16 PCM: стан між блоками, будь-яка довжина блоку."""

    def __init__(self, sample_rate, cutoff=CUTOFF_HZ, taps=TAPS):
        self.kernel = design(sample_rate, cutoff, taps)
        self.history = np.zeros(taps - 1)

    def process(self, pcm):
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if not len(x):
            return b""
        buf = np.concatenate((self.history, x))
        y = np.convolve(buf, self.kernel, mode="valid")
        self.history = buf[-len(self.history):]
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


def wrap(callback, sample_rate):
    """Callback PortAudio → той самий callback з відфільтрованим блоком."""
    flt = HighPass(sample_rate)

    def on_audio(indata, frame_count, time_info, status):
        callback(flt.process(bytes(indata)), frame_count, time_info, status)

    return on_audio
