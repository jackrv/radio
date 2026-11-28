"""Зріз гулу мережі (50 Гц і гармоніки 100/150) та вирізи дзижчання 250–550 Гц до VAD, ефіру й архіву.

FIR з вікном Хеммінга (numpy): зріз CUTOFF_HZ, 1023 відводи —
50/100/150 Гц глушаться на ~50 дБ, мова рації (від ~300 Гц) не зачіпається;
затримка ~32 мс. Вирізи NOTCH_HZ — IIR scipy (sosfilt), ~0,15 % CPU Pi. Вмикається галочкою в панелі (shared/audio_choice.py), діє з перезапуску ефіру.
"""
import numpy as np

CUTOFF_HZ = 200
TAPS = 1023
# Дзижчання вище зрізу: непарні гармоніки 50 Гц з USB-аудіо на Pi (28.11.2026, тиша — до 70 % енергії).
# Вирізи ±5 Гц: піки −13 дБ, мова −0,5 дБ (медіани на записах Pi); частота мережі гуляє на 1–1,5 Гц,
# тому вужчі вирізи промахуються, а стеження за частотою дало лише −2…−5 дБ (docs/ideas.md).
NOTCH_HZ = (250, 350, 450, 550)
NOTCH_WIDTH_HZ = 10


def notch_sections(sample_rate, freqs=NOTCH_HZ, width=NOTCH_WIDTH_HZ):
    """SOS вирізів або None, якщо scipy немає (тоді лише зріз нижче 200 Гц)."""
    try:
        from scipy.signal import iirnotch, tf2sos
    except ImportError:
        return None
    return np.vstack([tf2sos(*iirnotch(f, f / width, fs=sample_rate)) for f in freqs])


def design(sample_rate, cutoff=CUTOFF_HZ, taps=TAPS):
    n = np.arange(taps) - (taps - 1) / 2
    low = np.sinc(2 * cutoff / sample_rate * n) * np.hamming(taps)
    low /= low.sum()
    high = -low
    high[(taps - 1) // 2] += 1.0
    return high


class HighPass:
    """Потоковий фільтр int16 PCM: стан між блоками, будь-яка довжина блоку."""

    def __init__(self, sample_rate, cutoff=CUTOFF_HZ, taps=TAPS, notches=True):
        self.kernel = design(sample_rate, cutoff, taps)
        self.history = np.zeros(taps - 1)
        self.sos = notch_sections(sample_rate) if notches else None
        self.zi = None if self.sos is None else np.zeros((self.sos.shape[0], 2))

    def process(self, pcm):
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if not len(x):
            return b""
        buf = np.concatenate((self.history, x))
        y = np.convolve(buf, self.kernel, mode="valid")
        self.history = buf[-len(self.history):]
        if self.sos is not None:
            from scipy.signal import sosfilt
            y, self.zi = sosfilt(self.sos, y, zi=self.zi)
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


def wrap(callback, sample_rate):
    """Callback PortAudio → той самий callback з відфільтрованим блоком."""
    flt = HighPass(sample_rate)

    def on_audio(indata, frame_count, time_info, status):
        callback(flt.process(bytes(indata)), frame_count, time_info, status)

    return on_audio
