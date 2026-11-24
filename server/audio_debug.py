import os
import wave
from datetime import datetime

import numpy as np

from config import DEBUG_AUDIO_DIR


def save_debug_audio(pcm, audio, sample_rate, index):
    """Сохраняет исходный PCM и точный вход Whisper."""

    folder = DEBUG_AUDIO_DIR
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    name = (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + f"_{os.getpid()}_{index:02d}"
    )

    raw_path = folder / f"{name}_raw.wav"
    whisper_path = folder / f"{name}_whisper.wav"
    exact_path = folder / f"{name}_whisper.npy"

    def write_wav(path, data):
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(data)

    # Точные PCM-байты после VAD.
    write_wav(raw_path, pcm)

    # Точный float32-массив, переданный Whisper.
    np.save(exact_path, audio)

    # Прослушиваемая PCM16-версия нормализованного сигнала.
    processed = (
        np.rint(np.clip(audio, -1.0, 1.0) * 32767)
        .astype(np.int16)
        .tobytes()
    )
    write_wav(whisper_path, processed)

    return raw_path, whisper_path, exact_path
