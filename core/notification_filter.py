"""Консервативне відсіювання відомого окремого звуку за локальним WAV-зразком.

Близьке зіставлення хвилі лише знаходить кандидата. Увесь залишок сегмента,
включно з ділянкою сповіщення, перевіряється на додатковий звук. Сумнівний
сегмент залишається для Whisper; коротка тривалість сама по собі не є ознакою.
"""

from pathlib import Path
import wave

import numpy as np

MAX_TEMPLATES = 16
MAX_RECORDING_S = 5.0
MAX_SOUND_S = 2.0
MIN_SOUND_S = 0.04
QUIET_CONTEXT_S = 0.12
MIN_CORRELATION = 0.995
NOISE_MULTIPLIER = 2.5
PCM_FLOOR = 3 / 32768


def _window_sums(values, length):
    cumulative = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    return cumulative[length:] - cumulative[:-length]


def _max_rms(audio, frame):
    length = min(frame, len(audio))
    if length == 0:
        return 0.0
    return float(np.sqrt(max(0.0, np.max(_window_sums(audio * audio, length) / length))))


def _prepare_template(samples, sample_rate):
    audio = np.asarray(samples, dtype=np.float64)
    if (audio.ndim != 1 or not np.isfinite(audio).all()
            or not 0 < len(audio) <= MAX_RECORDING_S * sample_rate):
        raise ValueError("Некоректний або надто довгий зразок звуку")
    audio = audio - np.median(audio)
    frame = max(1, round(sample_rate * 0.02))
    padded = np.pad(audio, (0, -len(audio) % frame))
    rms = np.sqrt(np.mean(padded.reshape(-1, frame) ** 2, axis=1))
    active = np.flatnonzero(rms > max(float(np.max(rms)) * 0.03, PCM_FLOOR))
    if active.size == 0:
        raise ValueError("Зразок не містить звуку")
    # Залишаємо атаку й затухання, а довгі тихі краї зразка прибираємо.
    start = max(0, (int(active[0]) - 2) * frame)
    end = min(len(audio), (int(active[-1]) + 3) * frame)
    audio = audio[start:end]
    if not MIN_SOUND_S * sample_rate <= len(audio) <= MAX_SOUND_S * sample_rate:
        raise ValueError("Звук у зразку має тривати від 0,04 до 2 секунд")
    audio = audio - np.mean(audio)
    if np.dot(audio, audio) <= 1e-12:
        raise ValueError("Зразок не містить змінного аудіосигналу")
    audio.setflags(write=False)
    return audio


class NotificationSoundFilter:
    def __init__(self, templates, sample_rate):
        if not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("Некоректна частота дискретизації")
        if len(templates) > MAX_TEMPLATES:
            raise ValueError("Забагато зразків звуків")
        self.sample_rate = sample_rate
        self.templates = tuple(_prepare_template(t, sample_rate) for t in templates)

    @classmethod
    def from_directory(cls, directory, sample_rate, log):
        templates = []
        try:
            paths = sorted(Path(directory).glob("*.wav"))
            if len(paths) > MAX_TEMPLATES:
                log.warning("Фільтр сповіщень: використано лише перші %d WAV-зразків", MAX_TEMPLATES)
            for path in paths[:MAX_TEMPLATES]:
                try:
                    with wave.open(str(path), "rb") as recording:
                        if (recording.getnchannels() != 1 or recording.getsampwidth() != 2
                                or recording.getframerate() != sample_rate
                                or not 0 < recording.getnframes() <= MAX_RECORDING_S * sample_rate):
                            raise ValueError("Потрібен короткий моно WAV PCM16 із частотою приймача")
                        expected = recording.getnframes() * 2
                        raw = recording.readframes(recording.getnframes())
                    if len(raw) != expected:
                        raise ValueError("Неповний WAV-зразок")
                    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
                    templates.append(_prepare_template(samples, sample_rate))
                except (OSError, EOFError, wave.Error, ValueError):
                    # Ім’я файла й аудіодані не потрібні для діагностики відмови.
                    log.warning("Фільтр сповіщень: пропущено непридатний WAV-зразок")
        except OSError:
            log.warning("Фільтр сповіщень: каталог зразків недоступний")
        result = cls([], sample_rate)
        result.templates = tuple(templates)
        log.info("Фільтр звуків сповіщень: завантажено зразків — %d", len(templates))
        return result

    def is_notification(self, pcm):
        if (not self.templates or len(pcm) % 2
                or len(pcm) > MAX_RECORDING_S * self.sample_rate * 2):
            return False
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
        context = round(QUIET_CONTEXT_S * self.sample_rate)
        if len(audio) < 2 * context + round(MIN_SOUND_S * self.sample_rate):
            return False
        audio -= np.median(np.concatenate((audio[:context], audio[-context:])))
        edge_rms = [float(np.sqrt(np.mean(edge * edge))) for edge in (audio[:context], audio[-context:])]
        noise = max(min(edge_rms), PCM_FLOOR / NOISE_MULTIPLIER)
        residual_limit = max(PCM_FLOOR, NOISE_MULTIPLIER * noise)
        if max(edge_rms) > residual_limit:
            return False
        frame = max(1, round(0.02 * self.sample_rate))

        for template in self.templates:
            length = len(template)
            last = len(audio) - length - context
            if last < context:
                continue
            energy = float(np.dot(template, template))
            fft_size = 1 << (len(audio) + length - 2).bit_length()
            dots = np.fft.irfft(np.fft.rfft(audio, fft_size)
                                * np.conj(np.fft.rfft(template, fft_size)), fft_size)[:len(audio) - length + 1]
            sums = _window_sums(audio, length)
            variance = np.maximum(_window_sums(audio * audio, length) - sums * sums / length, 0)
            correlations = np.abs(dots) / np.sqrt(np.maximum(variance * energy, 1e-30))
            correlations[variance <= length * PCM_FLOOR ** 2] = 0
            start = context + int(np.argmax(correlations[context:last + 1]))
            if correlations[start] < MIN_CORRELATION:
                continue
            # Віднімаємо тільки відомий зразок. Мовлення до, після або поверх нього
            # лишається в залишку й забороняє відкидання всього сегмента.
            residual = audio.copy()
            residual[start:start + length] -= dots[start] / energy * template + sums[start] / length
            if _max_rms(residual, frame) <= residual_limit:
                return True
        return False
