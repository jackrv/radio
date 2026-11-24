import numpy as np


def apply_gain(indata, gain: float) -> bytes:
    """Применяет программное усиление к входящему PCM int16."""

    if gain == 1.0:
        return bytes(indata)

    audio = np.frombuffer(indata, dtype=np.int16).astype(np.float32) * gain
    return np.clip(audio, -32768, 32767).astype(np.int16).tobytes()


def prepare_audio(pcm, min_rms: float, normalize: bool):
    """Преобразует PCM int16 в float32 и подготавливает звук для Whisper."""

    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0

    if audio.size == 0:
        return audio, 0.0

    rms = float(np.sqrt(np.mean(audio ** 2)))

    if rms < min_rms:
        return audio, rms

    if normalize:
        peak = float(np.max(np.abs(audio)))
        if peak > 0:
            audio = audio / peak * 0.9

    return audio, rms
