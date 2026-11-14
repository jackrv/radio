"""Синтетичні звуки й тимчасові WAV-файли; справжнього радіоефіру немає."""

from unittest.mock import Mock
import wave

import numpy as np
import pytest

from core.notification_filter import MAX_TEMPLATES, NotificationSoundFilter

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


@pytest.fixture
def detector():
    return NotificationSoundFilter([phone_sound()], SR)


@pytest.mark.parametrize("gain", [0.15, 0.6, 1, -0.8])
@pytest.mark.parametrize("noise", [0, 0.0002])
def test_known_isolated_sound_at_different_levels_is_filtered(detector, gain, noise):
    audio = segment(phone_sound() * gain)
    audio += np.random.default_rng(8).normal(0, noise, len(audio))
    assert detector.is_notification(pcm(audio))


def test_template_with_quiet_edges_and_dc_offset_matches():
    detector = NotificationSoundFilter([segment() + 0.02], SR)
    assert detector.is_notification(pcm(segment() + 0.01))


@pytest.mark.parametrize("position", [0.04, 0.21, 0.5, 1.16, 1.52])
@pytest.mark.parametrize("level", [0.08, 0.001])
def test_additional_audio_before_during_or_after_notification_is_preserved(detector, position, level):
    audio = segment()
    speech = level * voice_like(0.1)
    start = round(position * SR)
    audio[start:start + len(speech)] += speech
    assert not detector.is_notification(pcm(audio))


@pytest.mark.parametrize("duration", [0.06, 0.2, 0.6, 1.5])
def test_short_speech_like_audio_is_preserved(detector, duration):
    assert not detector.is_notification(pcm(segment(0.1 * voice_like(duration))))


def test_unknown_notification_is_preserved(detector):
    t = np.arange(round(0.7 * SR)) / SR
    unknown = 0.2 * np.sin(2 * np.pi * 1200 * t) * np.exp(-2 * t)
    assert not detector.is_notification(pcm(segment(unknown)))


def test_partial_sound_without_quiet_context_is_preserved(detector):
    assert not detector.is_notification(pcm(phone_sound()))
    assert not detector.is_notification(pcm(segment(phone_sound()[:SR // 4])))


def test_two_occurrences_are_not_mistaken_for_one_isolated_sound(detector):
    sound = np.concatenate((phone_sound(), np.zeros(SR // 4), phone_sound()))
    assert not detector.is_notification(pcm(segment(sound)))


@pytest.mark.parametrize("audio", [np.zeros(SR), np.ones(SR) * 0.2, np.zeros(6 * SR)])
def test_silence_dc_and_long_segments_are_preserved(detector, audio):
    assert not detector.is_notification(pcm(audio))


def test_dc_step_is_not_a_notification(detector):
    assert not detector.is_notification(pcm(segment(np.ones(SR // 2) * 0.1)))


def test_noise_and_truncated_pcm_are_preserved(detector):
    noise = np.random.default_rng(17).normal(0, 0.05, 2 * SR)
    assert not detector.is_notification(pcm(noise))
    assert not detector.is_notification(b"\0")
    assert not detector.is_notification(b"")


def test_no_templates_never_blocks_audio():
    assert not NotificationSoundFilter([], SR).is_notification(pcm(segment()))


def write_wav(path, samples, *, rate=SR, channels=1):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm(samples))


def test_load_private_wav_from_directory(tmp_path):
    path = tmp_path / "phone.wav"
    write_wav(path, segment())
    detector = NotificationSoundFilter.from_directory(tmp_path, SR, Mock())
    assert len(detector.templates) == 1
    assert detector.is_notification(pcm(segment()))


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
    detector = NotificationSoundFilter.from_directory(tmp_path, SR, log)
    assert detector.templates == ()
    assert not detector.is_notification(pcm(segment()))
    log.warning.assert_called_once()


def test_corrupt_wav_and_unrelated_files_are_ignored(tmp_path):
    (tmp_path / "broken.wav").write_bytes(b"not a WAV")
    (tmp_path / "notes.txt").write_text("not an audio template")
    detector = NotificationSoundFilter.from_directory(tmp_path, SR, Mock())
    assert detector.templates == ()


def test_missing_directory_is_disabled_without_creating_files(tmp_path):
    directory = tmp_path / "absent"
    detector = NotificationSoundFilter.from_directory(directory, SR, Mock())
    assert detector.templates == () and not directory.exists()


def test_template_limit_is_enforced(tmp_path):
    for index in range(MAX_TEMPLATES + 1):
        write_wav(tmp_path / f"{index:02}.wav", phone_sound())
    detector = NotificationSoundFilter.from_directory(tmp_path, SR, Mock())
    assert len(detector.templates) == MAX_TEMPLATES
    with pytest.raises(ValueError):
        NotificationSoundFilter([phone_sound()] * (MAX_TEMPLATES + 1), SR)


@pytest.mark.parametrize("samples", [np.array([]), np.array([np.nan] * SR), np.zeros((2, SR))])
def test_malformed_arrays_cannot_be_registered(samples):
    with pytest.raises(ValueError):
        NotificationSoundFilter([samples], SR)
