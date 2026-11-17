"""Валідація WAV і ліміти спільних зразків спектрального фільтра."""
import wave
from unittest.mock import Mock
import numpy as np
import pytest
from core.notification_templates import MAX_TEMPLATES, load_templates, _prepare_template
from core.notification_spectral import SpectralNotificationMatcher
from tests.audio_helpers import SR, pcm, phone_sound, segment


def write_wav(path, samples, *, rate=SR, channels=1):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm(samples))


def test_load_private_wav_from_directory(tmp_path):
    path = tmp_path / "phone.wav"
    write_wav(path, segment())
    templates = load_templates(tmp_path, SR, Mock())
    assert len(templates) == 1
    assert not templates[0].flags.writeable


@pytest.mark.parametrize("rate,channels,samples", [
    (8000, 1, phone_sound()),
    (SR, 2, phone_sound()),
    (SR, 1, np.zeros(SR)),
    (SR, 1, np.ones(SR) * 0.1),
    (SR, 1, np.ones(6 * SR)),
    (SR, 1, 0.2 * np.sin(np.arange(3 * SR))),
])
def test_invalid_templates_are_skipped_without_disabling_recognition(tmp_path, rate, channels, samples):
    write_wav(tmp_path / "invalid.wav", samples, rate=rate, channels=channels)
    log = Mock()
    templates = load_templates(tmp_path, SR, log)
    assert templates == []
    log.warning.assert_called_once()


def test_corrupt_wav_and_unrelated_files_are_ignored(tmp_path):
    (tmp_path / "broken.wav").write_bytes(b"not a WAV")
    (tmp_path / "notes.txt").write_text("not an audio template")
    templates = load_templates(tmp_path, SR, Mock())
    assert templates == []


def test_missing_directory_is_disabled_without_creating_files(tmp_path):
    directory = tmp_path / "absent"
    templates = load_templates(directory, SR, Mock())
    assert templates == [] and not directory.exists()


def test_template_limit_is_enforced(tmp_path):
    for index in range(MAX_TEMPLATES + 1):
        write_wav(tmp_path / f"{index:02}.wav", phone_sound())
    templates = load_templates(tmp_path, SR, Mock())
    assert len(templates) == MAX_TEMPLATES
    with pytest.raises(ValueError):
        SpectralNotificationMatcher([phone_sound()] * (MAX_TEMPLATES + 1), SR)


@pytest.mark.parametrize("samples", [np.array([]), np.array([np.nan] * SR), np.zeros((2, SR))])
def test_malformed_arrays_cannot_be_registered(samples):
    with pytest.raises(ValueError):
        _prepare_template(samples, SR)


