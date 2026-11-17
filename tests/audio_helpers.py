"""Синтетичні аудіозразки для тестів; без справжнього ефіру."""

import numpy as np

SR = 16000


def phone_sound():
    t = np.arange(round(0.7 * SR)) / SR
    envelope = np.minimum(t / 0.012, 1) * np.exp(-3 * t)
    frequency = np.where(t < 0.3, 730, 980)
    phase = 2 * np.pi * np.cumsum(frequency) / SR
    return 0.25 * envelope * (np.sin(phase) + 0.25 * np.sin(2 * phase))


def pcm(audio):
    return np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def segment(sound=None):
    if sound is None:
        sound = phone_sound()
    return np.concatenate((np.zeros(round(0.4 * SR)), sound, np.zeros(round(0.6 * SR))))


def voice_like(duration):
    # Змінні гармоніки та широкосмугова складова: тест домішки, не еталон живого мовлення.
    t = np.arange(round(duration * SR)) / SR
    envelope = np.sin(np.pi * np.linspace(0, 1, len(t))) ** 2
    noise = np.random.default_rng(345).normal(0, 0.2, len(t))
    return envelope * (np.sin(2 * np.pi * 137 * t) + 0.4 * np.sin(2 * np.pi * 281 * t) + noise)

