"""Читання мікшера (amixer) і зріз гулу: розбір, збереження галочки, фільтр."""
import numpy as np
import pytest

from shared import audio_choice as a
from shared import highpass, mixer

AMIXER = """Simple mixer control 'Headset',0
  Capabilities: pvolume cvolume cvolume-joined pswitch pswitch-joined cswitch cswitch-joined
  Playback channels: Front Left - Front Right
  Capture channels: Mono
  Limits: Playback 0 - 120 Capture 0 - 120
  Mono: Capture 96 [80%] [-6.00dB] [on]
  Front Left: Playback 120 [100%] [0.00dB] [on]
Simple mixer control 'PCM',0
  Capabilities: pvolume
  Playback channels: Mono
  Limits: Playback 0 - 31
  Mono: Playback 20 [65%]
"""
DEVICE = "Test USB Audio: - (hw:1,0)"


def test_parse_keeps_only_capture_controls():
    assert mixer.parse(AMIXER) == [{"name": "Headset", "index": 0, "switch": True,
                                    "min": 0, "max": 120, "value": 96, "db": -6.0}]


def test_card_comes_from_hw_or_plughw_name():
    assert mixer.card_of(DEVICE) == 1
    assert mixer.card_of("X: - (plughw:2,0)") == 2


def test_highpass_flag_survives_device_save(tmp_path):
    assert a.highpass(tmp_path) is False
    a.set_highpass(tmp_path, True)
    a.write(tmp_path, {"RADIO_INPUT_DEVICE": "", "RADIO_OUTPUT_DEVICE": ""}, 0)
    assert a.highpass(tmp_path) is True
    with pytest.raises(ValueError):
        a.set_highpass(tmp_path, "yes")


def level_db(hz, sr=16000):
    t = np.arange(sr * 2) / sr
    x = (np.sin(2 * np.pi * hz * t) * 10000).astype(np.int16)
    flt = highpass.HighPass(sr)
    # Блоками різної довжини — як віддає PortAudio на Pi.
    out = b"".join(flt.process(x[i:i + n].tobytes()) for i, n in zip(range(0, len(x), 700), [700] * 100))
    y = np.frombuffer(out, np.int16)[sr:].astype(float)
    return 20 * np.log10(np.sqrt((y ** 2).mean()) / (10000 / np.sqrt(2)))


def test_highpass_removes_mains_hum_and_keeps_speech_band():
    for hz in (50, 100, 150):
        assert level_db(hz) < -40
    for hz in (300, 1000, 3000):
        assert abs(level_db(hz)) < 0.5


def test_wrap_filters_block_before_callback():
    got = []
    cb = highpass.wrap(lambda data, n, t, s: got.append((len(data), n)), 16000)
    cb(np.zeros(480, np.int16).tobytes(), 480, None, None)
    assert got == [(960, 480)]


def test_notches_cut_the_buzz_between_speech_frequencies():
    pytest.importorskip("scipy")
    for hz in (250, 350, 450, 550):
        assert level_db(hz) < -20
    for hz in (300, 400, 1000):
        assert abs(level_db(hz)) < 0.5


def test_without_scipy_only_the_cut_below_200_works(monkeypatch):
    monkeypatch.setattr(highpass, "notch_sections", lambda *a, **k: None)
    flt = highpass.HighPass(16000)
    assert flt.sos is None and len(flt.process(np.zeros(480, np.int16).tobytes())) == 960
