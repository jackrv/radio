"""Підготовка приватних WAV-зразків для спектрального фільтра."""

from pathlib import Path
import wave

import numpy as np

MAX_TEMPLATES = 16
MAX_RECORDING_S = 5.0
MAX_SOUND_S = 2.0
MIN_SOUND_S = 0.04
PCM_FLOOR = 3 / 32768


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


def load_templates(directory, sample_rate, log):
    """Підготовлені (обрізані) зразки з каталогу; для спектрального фільтра."""
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
    return templates

