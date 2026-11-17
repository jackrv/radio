"""Спектральне зіставлення звуків сповіщень (log-mel відбиток).

На відміну від хвильового фільтра порівнює не форму хвилі, а спектр у часі.
Тому гучність, луна від кімнати, стиснення Telegram і зсув фази майже не
впливають на оцінку. Окремо перевіряється, що звук «сам у кадрі»: немає
активного звуку поза збігом і немає сторонньої енергії в смугах, де зразок
тихий (мовлення поверх сигналу).

Режим задає config.SPECTRAL_NOTIFICATION_MODE: active відкидає кандидата,
shadow лише записує оцінку, off вимикає перевірку. Поточні межі обираються
за розміченими реальними записами.
"""

import hashlib

import numpy as np

from core.notification_templates import MAX_TEMPLATES, _prepare_template, load_templates

N_FFT = 512
WINDOW = 400          # 25 мс при 16 кГц
HOP = 160             # 10 мс
N_MELS = 40
F_MIN, F_MAX = 125.0, 7600.0
DYNAMIC_RANGE_DB = 40.0
NOISE_PERCENTILE = 20
MIN_TEMPLATE_FRAMES = 3
# Довші сегменти не оцінюються: окремий короткий звук не тягне на 10 секунд.
MAX_SCORE_S = 10.0
EDGE_PAD_FRAMES = 3
ACTIVE_ABOVE_FLOOR_DB = 10.0
# Звук на 35 дБ тихіший за сам сигнал (хвіст луни) — не «інший звук у кадрі».
ACTIVE_BELOW_PEAK_DB = 35.0
STRONG_BAND_DB = 20.0
EPS = 1e-12

DEFAULT_THRESHOLD = 0.85
DEFAULT_MAX_OUTSIDE_S = 0.2
DEFAULT_MAX_EXTRA = 0.02
MODES = ("off", "shadow", "active")

_FILTERS = {}


def _mel_filters(sample_rate):
    if sample_rate not in _FILTERS:
        def hz_to_mel(f):
            return 2595.0 * np.log10(1.0 + f / 700.0)

        def mel_to_hz(m):
            return 700.0 * (10 ** (m / 2595.0) - 1.0)

        top = min(F_MAX, sample_rate / 2)
        edges = mel_to_hz(np.linspace(hz_to_mel(F_MIN), hz_to_mel(top), N_MELS + 2))
        bins = np.fft.rfftfreq(N_FFT, 1.0 / sample_rate)
        bank = np.zeros((N_MELS, len(bins)))
        for band in range(N_MELS):
            low, centre, high = edges[band:band + 3]
            rising = (bins - low) / max(centre - low, EPS)
            falling = (high - bins) / max(high - centre, EPS)
            bank[band] = np.clip(np.minimum(rising, falling), 0, None)
        bank.setflags(write=False)
        _FILTERS[sample_rate] = bank
    return _FILTERS[sample_rate]


def mel_power(audio, sample_rate):
    """Потужність у мел-смугах: масив (кадри, N_MELS). Кадри по центру, крок 10 мс."""
    audio = np.asarray(audio, dtype=np.float64)
    padded = np.pad(audio, (WINDOW // 2, WINDOW // 2))
    if len(padded) < WINDOW:
        padded = np.pad(padded, (0, WINDOW - len(padded)))
    frames = np.lib.stride_tricks.sliding_window_view(padded, WINDOW)[::HOP] * np.hanning(WINDOW)
    spectrum = np.abs(np.fft.rfft(frames, N_FFT)) ** 2
    return spectrum @ _mel_filters(sample_rate).T


def _decibels(power):
    level = 10.0 * np.log10(power + EPS)
    # Відносний поріг: тиха частина не домінує над формою сигналу за будь-якої гучності.
    return np.maximum(level, float(np.max(level)) - DYNAMIC_RANGE_DB)


def template_hash(prepared):
    return hashlib.sha256(np.ascontiguousarray(prepared, dtype=np.float64).tobytes()).hexdigest()


class _Template:
    __slots__ = ("power", "centred", "norm", "strong", "bands", "frames", "digest")

    def __init__(self, prepared, sample_rate):
        self.power = mel_power(prepared, sample_rate)
        level = _decibels(self.power)
        self.frames = level.shape[0]
        self.centred = level - level.mean()
        self.norm = float(np.linalg.norm(self.centred))
        self.strong = level >= float(np.max(level)) - STRONG_BAND_DB
        # Смуги, де звук є хоч колись (з урахуванням луни й затухання в кімнаті).
        self.bands = level.max(axis=0) >= float(np.max(level)) - STRONG_BAND_DB
        self.digest = template_hash(prepared)


class SpectralNotificationMatcher:
    def __init__(self, templates, sample_rate, *, prepared=False,
                 threshold=DEFAULT_THRESHOLD, max_outside_s=DEFAULT_MAX_OUTSIDE_S,
                 max_extra=DEFAULT_MAX_EXTRA):
        if not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("Некоректна частота дискретизації")
        if len(templates) > MAX_TEMPLATES:
            raise ValueError("Забагато зразків звуків")
        self.sample_rate = sample_rate
        self.threshold = float(threshold)
        self.max_outside_s = float(max_outside_s)
        self.max_extra = float(max_extra)
        items = []
        for template in templates:
            ready = template if prepared else _prepare_template(template, sample_rate)
            item = _Template(ready, sample_rate)
            if item.frames >= MIN_TEMPLATE_FRAMES and item.norm > EPS:
                items.append(item)
        self.templates = tuple(items)

    @classmethod
    def from_directory(cls, directory, sample_rate, log, **limits):
        return cls(load_templates(directory, sample_rate, log), sample_rate, prepared=True, **limits)

    def is_candidate(self, score, outside_s, extra_ratio, threshold=None):
        limit = self.threshold if threshold is None else threshold
        return score >= limit and outside_s <= self.max_outside_s and extra_ratio <= self.max_extra

    def score_pcm(self, pcm, exclude=()):
        if len(pcm) % 2:
            return None
        return self.score(np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768, exclude)

    def score(self, audio, exclude=()):
        """Найкращий збіг або None (немає зразків, сегмент задовгий чи закороткий).

        ``exclude`` — відбитки зразків, які не враховуються (оцінка запису,
        з якого цей зразок і зроблено).
        """
        templates = [t for t in self.templates if t.digest not in exclude]
        if not templates or not 0 < len(audio) <= MAX_SCORE_S * self.sample_rate:
            return None
        audio = np.asarray(audio, dtype=np.float64)
        audio = audio - np.median(audio)
        power = mel_power(audio, self.sample_rate)
        # Стаціонарний фон (шипіння ефіру, гул) віднімається по смугах: інакше тихі
        # смуги зразка «чисті», а в записі там шум, і кореляція падає без причини.
        noise = np.percentile(power, NOISE_PERCENTILE, axis=0)
        level = _decibels(np.clip(power - noise, 0.0, None))
        frames = level.shape[0]
        row_sum = np.concatenate(([0.0], np.cumsum(level.sum(axis=1))))
        row_square = np.concatenate(([0.0], np.cumsum((level * level).sum(axis=1))))

        best = None
        for index, template in enumerate(self.templates):
            if template.digest in exclude or template.frames > frames:
                continue
            m = template.frames
            # Кореляція Пірсона кожного вікна з центрованим відбитком; зсув у дБ (гучність) зникає.
            dots = np.zeros(frames - m + 1)
            for band in range(N_MELS):
                dots += np.correlate(level[:, band], template.centred[:, band], mode="valid")
            count = m * N_MELS
            sums = row_sum[m:] - row_sum[:-m]
            variance = np.maximum(row_square[m:] - row_square[:-m] - sums * sums / count, 0.0)
            scores = np.where(variance > EPS, dots / (np.sqrt(variance) * template.norm + EPS), 0.0)
            start = int(np.argmax(scores))
            value = float(scores[start])
            if best is None or value > best[0]:
                best = (value, index, start, template)
        if best is None:
            return None

        value, index, start, template = best
        m = template.frames
        frame_s = HOP / self.sample_rate

        # 1) Активний звук поза збігом (мовлення до чи після сигналу).
        energy = 10.0 * np.log10(power.sum(axis=1) + EPS)
        floor = float(np.percentile(energy, NOISE_PERCENTILE))
        inside = np.zeros(frames, dtype=bool)
        inside[max(0, start - EDGE_PAD_FRAMES):start + m + EDGE_PAD_FRAMES] = True
        peak = float(np.max(energy[start:start + m]))
        active = energy > max(floor + ACTIVE_ABOVE_FLOOR_DB, peak - ACTIVE_BELOW_PEAK_DB)
        outside_s = float(np.count_nonzero(active & ~inside)) * frame_s

        # 2) Стороння енергія поверх сигналу в смугах, де самого звуку немає взагалі.
        #    Шум оцінюється за кадрами поза збігом (верхній дециль), щоб його
        #    випадкові сплески не вважалися мовленням.
        window = power[start:start + m]
        strong, bands = template.strong, template.bands
        gain = float(np.median(window[strong] / np.maximum(template.power[strong], EPS))) if strong.any() else 0.0
        if np.count_nonzero(~inside) >= 10:
            ceiling = np.percentile(power[~inside], 90, axis=0)
        else:
            ceiling = 3.0 * noise
        residual = np.clip(window - gain * template.power - ceiling, 0.0, None)
        signal = float(window[:, bands].sum())
        extra = float(residual[:, ~bands].sum()) / signal if signal > EPS and (~bands).any() else 0.0

        score = max(-1.0, min(1.0, value))
        return {
            "score": round(score, 4),
            "template": index,
            "offset_s": round(start * frame_s, 3),
            "match_s": round(m * frame_s, 3),
            "outside_s": round(outside_s, 3),
            "extra_ratio": round(min(extra, 1.0), 4),
            "candidate": self.is_candidate(score, outside_s, extra),
        }
