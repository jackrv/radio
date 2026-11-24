"""Акустична місткість тексту — вимірювання, не автоматичний фільтр.

Активна енергія та періодичність не дорівнюють мовленню. Порівняння з ними
потрібне для перевірки гіпотез на еталонах; результат не дозволяє видаляти слова.
Жодних моделей, VAD, SQLite чи зміни аудіо.
"""
import math
import re

import numpy as np

FRAME_MS = 30
ACTIVITY_DB = (3, 6, 9, 12, 15)
PERIODICITY = (0.3, 0.5, 0.7)
_WORDS = re.compile(r"[^\W_]+(?:[’'ʼ`][^\W_]+)*")


def word_count(text):
    """Числова група — один токен; це не кількість вимовлених слів/складів."""
    return len(_WORDS.findall(text or ""))


def measure_audio(pcm, sample_rate):
    """PCM16 mono. Повертає лише агрегати, без сигналу, спектра чи тексту.

    Повні 30-мс кадри; залишок явно зазначено. FFT пакетами до 256 кадрів,
    щоб довгий WAV не створював великих проміжних матриць.
    """
    if type(sample_rate) is not int or sample_rate not in (8000, 16000, 32000, 48000):
        raise ValueError('Потрібна частота 8000, 16000, 32000 або 48000 Гц')
    if len(pcm) % 2:
        raise ValueError('Очікується PCM16 із парною кількістю байтів')
    x = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768
    size = sample_rate * FRAME_MS // 1000
    count = len(x) // size
    result = {'duration_s': len(x) / sample_rate, 'frame_ms': FRAME_MS,
              'analysed_s': count * FRAME_MS / 1000,
              'tail_s': (len(x) - count * size) / sample_rate,
              'available': bool(count)}
    if not count:
        return result
    frames = x[:count * size].reshape(-1, size)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    floor = float(np.percentile(rms, 20))
    activity = {str(db): float(np.count_nonzero(rms > max(floor * 10 ** (db / 20), 1 / 32768)))
                * FRAME_MS / 1000 for db in ACTIVITY_DB}
    nfft = 1 << (2 * size - 1).bit_length()
    bins = np.fft.rfftfreq(nfft, 1 / sample_rate)
    band = (bins >= 300) & (bins <= min(3400, sample_rate / 2))
    periodic, flatness = [], []
    for start in range(0, count, 256):
        batch = frames[start:start + 256]
        centered = batch - batch.mean(axis=1, keepdims=True)
        power = np.abs(np.fft.rfft(centered, n=nfft)) ** 2
        correlation = np.fft.irfft(power, n=nfft)[:, :size]
        periodic.append(np.max(correlation[:, sample_rate // 400:sample_rate // 70], axis=1)
                        / np.maximum(correlation[:, 0], 1e-12))
        voiceband = power[:, band] + 1e-12
        flatness.append(np.exp(np.mean(np.log(voiceband), axis=1)) / np.mean(voiceband, axis=1))
    periodic = np.concatenate(periodic)
    active = rms > max(floor * 2, 1 / 32768)
    result.update(floor_rms=floor, activity_s=activity,
                  periodic_s={str(t): float(np.count_nonzero((periodic > t) & active))
                              * FRAME_MS / 1000 for t in PERIODICITY},
                  flatness_median=float(np.median(np.concatenate(flatness))),
                  dynamic_db=float(20 * np.log10(max(float(np.percentile(rms, 90)), 1e-10)
                                                 / max(float(np.percentile(rms, 10)), 1e-10))),
                  crest_db=float(20 * np.log10(max(float(np.max(np.abs(x))), 1e-10)
                                               / max(float(np.sqrt(np.mean(x * x))), 1e-10))))
    return result


def capacity_checks(words, metrics, *, min_words=12, max_words_per_s=5.0, extra_words=3):
    """Дослідницькі прапорці; спільний бюджет, різні оцінки активного часу.

    Межі за замовчуванням відповідають бюджетній частині looks_like_garbage.
    Нові енергетичні варіанти НЕ підключені до неї чи живого приймача.
    """
    if (type(words) is not int or words < 0 or type(min_words) is not int or min_words < 1
            or type(extra_words) is not int or extra_words < 0
            or isinstance(max_words_per_s, bool) or not isinstance(max_words_per_s, (int, float))
            or not math.isfinite(max_words_per_s) or max_words_per_s <= 0):
        raise ValueError('Некоректний бюджет слів')
    if not metrics.get('available'):
        return {}
    durations = {'whole_audio': metrics['duration_s'],
                 **{'energy_' + key: value for key, value in metrics['activity_s'].items()},
                 'periodic_03': metrics['periodic_s']['0.3']}
    return {name: {'activity_s': seconds,
                   'word_budget': seconds * max_words_per_s + extra_words,
                   'exceeds': words >= min_words and words > seconds * max_words_per_s + extra_words}
            for name, seconds in durations.items()}
