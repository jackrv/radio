import numpy as np

from core.audio import apply_gain, prepare_audio


def test_apply_gain_one_preserves_pcm():
    pcm = np.array([1000, -1000, 2000], dtype=np.int16).tobytes()

    assert apply_gain(pcm, 1.0) == pcm


def test_apply_gain_changes_amplitude():
    pcm = np.array([1000, -1000], dtype=np.int16).tobytes()

    result = np.frombuffer(apply_gain(pcm, 2.0), dtype=np.int16)

    assert result.tolist() == [2000, -2000]


def test_apply_gain_clips_int16_overflow():
    pcm = np.array([20000, -20000], dtype=np.int16).tobytes()

    result = np.frombuffer(apply_gain(pcm, 2.0), dtype=np.int16)

    assert result.tolist() == [32767, -32768]


def test_prepare_audio_calculates_rms():
    samples = np.array([16384, -16384], dtype=np.int16)
    audio, rms = prepare_audio(samples.tobytes(), 0.0, False)

    assert np.allclose(audio, [0.5, -0.5])
    assert abs(rms - 0.5) < 1e-6


def test_prepare_audio_normalizes_peak():
    samples = np.array([8192, -16384], dtype=np.int16)
    audio, rms = prepare_audio(samples.tobytes(), 0.0, True)

    assert abs(rms - np.sqrt(0.15625)) < 1e-6
    assert np.isclose(np.max(np.abs(audio)), 0.9)


def test_prepare_audio_handles_empty_pcm():
    audio, rms = prepare_audio(b"", 0.02, True)

    assert audio.size == 0
    assert rms == 0.0


def test_prepare_audio_handles_silence():
    pcm = np.zeros(1600, dtype=np.int16).tobytes()

    audio, rms = prepare_audio(pcm, 0.02, True)

    assert np.all(audio == 0)
    assert rms == 0.0
