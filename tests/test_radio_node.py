import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared import audio_devices as ad
from shared import ptt_key as pk

DEVICES = [
    {"name": "Built-in Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "USB Audio Device", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "USB Audio Device Pro", "max_input_channels": 0, "max_output_channels": 2},
]


def test_find_device_by_name_and_kind():
    assert ad.find_device("", "input", DEVICES) is None
    assert ad.find_device("usb", "input", DEVICES) == 1
    assert ad.find_device("usb audio device pro", "output", DEVICES) == 2
    assert ad.find_device("USB Audio Device", "output", DEVICES) == 1  # точний збіг важливіший
    with pytest.raises(ad.DeviceNotFound):
        ad.find_device("missing", "output", DEVICES)
    with pytest.raises(ad.DeviceNotFound):
        ad.find_device("pro", "input", DEVICES)


def test_saved_hw_name_finds_plughw_device():
    devices = [{"name": "Test USB Audio: - (plughw:0,0)", "max_input_channels": 2, "max_output_channels": 2}]
    assert ad.find_device("Test USB Audio: - (hw:0,0)", "input", devices) == 0
    assert ad.find_device("test usb audio: - (plughw:0,0)", "input", devices) == 0
    # Назва зайнятої карти з /proc: інший підпис, той самий порт.
    assert ad.find_device("Test USB Audio: USB Audio (hw:0,0)", "input", devices) == 0
    with pytest.raises(ad.DeviceNotFound):
        ad.find_device("Test USB Audio: USB Audio (hw:1,0)", "input", devices)


def test_rechunking_callback_emits_equal_frames_with_shifted_adc_time():
    got = []
    cb = ad.rechunking_callback(lambda d, n, t, s: got.append((d, n, t.inputBufferAdcTime, s)), 4, 1000)
    samples = bytes(range(20))  # 10 кадрів int16
    cb(samples[:6], 3, SimpleNamespace(currentTime=5.0, inputBufferAdcTime=1.0), "overflow")
    assert got == []
    cb(samples[6:], 7, SimpleNamespace(currentTime=5.0, inputBufferAdcTime=1.003), None)
    assert [g[0] for g in got] == [samples[:8], samples[8:16]]
    assert [g[1] for g in got] == [4, 4]
    assert got[0][2] == pytest.approx(1.0) and got[1][2] == pytest.approx(1.004)
    assert [g[3] for g in got] == ["overflow", None]
    cb(samples[:4], 2, SimpleNamespace(currentTime=6.0, inputBufferAdcTime=1.010), None)
    assert got[-1][0] == samples[16:20] + samples[:4]


def test_rechunk_only_on_linux():
    assert ad.rechunk_on_this_platform("linux")
    assert not ad.rechunk_on_this_platform("darwin")


def test_configured_env_overrides_config():
    cfg = SimpleNamespace(RADIO_OUTPUT_DEVICE="Speakers")
    assert ad.configured("RADIO_OUTPUT_DEVICE", cfg, env={}) == "Speakers"
    assert ad.configured("RADIO_OUTPUT_DEVICE", cfg, env={"RADIO_OUTPUT_DEVICE": " USB "}) == "USB"
    assert ad.configured("RADIO_PTT", cfg, env={}) == ""


def test_make_key_parses_specs(monkeypatch):
    made = []
    monkeypatch.setattr(pk, "GpioKey", lambda line, chip="/dev/gpiochip0", active_low=False: made.append((line, chip, active_low)))
    monkeypatch.setattr(pk, "SerialKey", lambda port, signal="rts", inverted=False: made.append((port, signal, inverted)))
    assert isinstance(pk.make_key("none"), pk.NoKey)
    assert isinstance(pk.make_key(""), pk.NoKey)
    pk.make_key("gpio:17")
    pk.make_key("gpio!:gpiochip4:27")
    pk.make_key("serial:/dev/ttyUSB0:dtr")
    pk.make_key("serial:/dev/ttyUSB0")
    assert made == [("17", "/dev/gpiochip0", False), ("27", "/dev/gpiochip4", True),
                    ("/dev/ttyUSB0", "dtr", False), ("/dev/ttyUSB0", "rts", False)]
    with pytest.raises(ValueError):
        pk.make_key("relay:1")


class FakeKey:
    name = "fake"

    def __init__(self):
        self.events = []

    def press(self):
        self.events.append("press")

    def release(self):
        self.events.append("release")

    def close(self):
        self.events.append("close")


def test_guarded_key_force_releases_after_max_hold():
    key = FakeKey()
    guard = pk.GuardedKey(key, max_hold_s=0.1, log=Mock())
    guard.press()
    guard.press()  # повторне натискання не дублює
    time.sleep(0.25)
    assert key.events == ["press", "release"] and not guard.pressed
    guard.close()
    assert key.events[-1] == "close"


def test_transmit_presses_key_around_audio_and_releases_on_error():
    from shared import radio_stream as rs
    order = []

    class Player:
        def __init__(self, sr):
            pass

        def __enter__(self):
            return lambda pcm: order.append("audio")

        def __exit__(self, *exc):
            return False

    key = FakeKey()
    key.press = lambda: order.append("press")
    key.release = lambda: order.append("release")
    stream = rs.RadioStream(sample_rate=16000, host="127.0.0.1", port=0, transmit=True,
                            player_factory=Player, ptt_key=key, ptt_lead_s=0.01, ptt_tail_s=0.01)
    stream.transmit_session(iter([b"\0\0", b"\0\0"]))
    assert order == ["press", "audio", "audio", "release"]

    order.clear()

    def broken():
        yield b"\0\0"
        raise OSError("synthetic")

    with pytest.raises(OSError):
        stream.transmit_session(broken())
    assert order == ["press", "audio", "release"]
    assert not stream._tx_active and stream.input_muted()  # хвіст напівдуплексу


def test_radio_node_wires_stream_and_forwards_segments():
    from server import radio_node
    captured = {}

    class FakeSupervisor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    stream = SimpleNamespace(live_audio=Mock(), input_muted=Mock(return_value=False),
                             set_signal_matcher=Mock())
    cfg = SimpleNamespace(SR=16000, GAIN=1.0, FRAME_MS=30, FRAME_LEN=480, VAD_LEVEL=2, PREROLL_MS=500,
                          SILENCE_END_MS=600, MIN_SPEECH_MS=500, MAX_SEGMENT_S=20,
                          NOTIFICATION_SOUNDS_DIR="/nonexistent", SPECTRAL_NOTIFICATION_THRESHOLD=0.74,
                          SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S=0.6, SPECTRAL_NOTIFICATION_MAX_EXTRA=0.15,
                          SPECTRAL_NOTIFICATION_MODE="active", RADIO_INPUT_DEVICE="")
    sink = []
    supervisor, got = radio_node.build_node(cfg, logging.getLogger("test"), segment_sink=sink.append,
                                            stream_starter=lambda c, l, **kw: stream, supervisor_cls=FakeSupervisor)
    assert got is stream and captured["on_live_audio"] is stream.live_audio
    assert captured["input_muted"] is stream.input_muted and captured["telegram_listener"] is None
    stream.set_signal_matcher.assert_called_once()
    captured["segments"].put(b"\1\0")
    import threading
    worker = threading.Thread(target=captured["transcriber"], daemon=True)
    worker.start()
    deadline = time.monotonic() + 2
    while not sink and time.monotonic() < deadline:
        time.sleep(0.02)
    captured["transcriber_stop"]()
    worker.join(2)
    assert sink == [b"\1\0"]

    none, _ = radio_node.build_node(cfg, logging.getLogger("test"), stream_starter=lambda c, l, **kw: None,
                                    supervisor_cls=FakeSupervisor)
    assert none is None


def test_busy_card_hidden_by_portaudio_is_listed(tmp_path):
    (tmp_path / "cards").write_text(" 0 [CAM            ]: USB-Audio - Test Card\n                      Test Card at usb\n")
    (tmp_path / "pcm").write_text("00-00: USB Audio : USB Audio : capture 1\n01-00: HDMI : HDMI : playback 1\n")
    status = tmp_path / "card0/pcm0c/sub0/status"
    status.parent.mkdir(parents=True)
    status.write_text("state: RUNNING\n")
    found = {"inputs": [], "outputs": []}
    ad.add_busy(found, tmp_path)
    ad.add_busy(found, tmp_path)   # без дублів
    assert [d["name"] for d in found["inputs"]] == ["Test Card: USB Audio (hw:0,0)"] and found["outputs"] == []
    status.write_text("closed\n")
    free = {"inputs": [], "outputs": []}
    ad.add_busy(free, tmp_path)
    assert free["inputs"] == []


def _gate(limit):
    import numpy as np
    from server.live_gate import LiveGate
    out = []
    gate = LiveGate(lambda pcm, phase: out.append((len(pcm) // 2, phase)), lambda: limit[0], 16000)
    frame = lambda value, n=480: np.full(n, int(value * 32768), np.int16).tobytes()
    return gate, out, frame


def test_live_gate_drops_quiet_and_keeps_loud_with_its_beginning():
    gate, out, frame = _gate([0.03])
    gate(frame(0.01, 8480), "start")
    for _ in range(40):
        gate(frame(0.01), "frame")
    gate(frame(0.01), "end")
    assert out == []
    gate(frame(0.005, 8000) + frame(0.1), "start")
    for _ in range(9):
        gate(frame(0.1), "frame")
    gate(frame(0.005), "end")
    assert out[0] == (8480, "start") or out[0][1] == "start" and out[0][0] >= 8480, "початок не зрізано"
    assert [p for _, p in out].count("start") == 1 and out[-1] == (480, "end")
    assert sum(n for n, _ in out) == 8480 + 10 * 480


def test_live_gate_opens_late_without_a_long_backlog():
    gate, out, frame = _gate([0.03])
    gate(frame(0.01, 8480), "start")
    for _ in range(200):
        gate(frame(0.01), "frame")
    while not out:
        gate(frame(0.1), "frame")
    assert out[0][1] == "start" and out[0][0] <= 16000 * 0.8 + 480


def test_live_gate_off_is_transparent_and_threshold_is_live():
    limit = [0.0]
    gate, out, frame = _gate(limit)
    gate(frame(0.001), "start")
    gate(frame(0.001), "frame")
    gate(frame(0.001), "end")
    assert out == [(480, "start"), (480, "frame"), (480, "end")]
    limit[0] = 0.03
    gate(frame(0.001), "start")
    assert len(out) == 3
    limit[0] = 0.0
    gate(frame(0.001), "frame")
    assert out[3] == (960, "start")


def test_live_threshold_reaches_the_node(tmp_path):
    from server.node_control import NodeControl, write_json
    control = NodeControl(tmp_path)
    assert control.live_min_rms() == 0.0
    for value, want in ((0.03, 0.03), (5, 0.0), ("x", 0.0), (True, 0.0)):
        write_json(control.path, {"LIVE_MIN_RMS": value})
        control._checked = float("-inf")
        assert control.live_min_rms() == want
