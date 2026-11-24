"""Мікрофон приймача закривається, поки в панелі звучить архівний запис (без луни)."""

import queue
from unittest.mock import Mock, patch

import pytest

from server.pause_gate import PauseGate
from server.supervisor import ApplicationSupervisor
from shared.receiver_control import reception_request, set_listening_hold, set_pause_minutes


def test_hold_expires_by_itself_and_operator_pause_has_priority(tmp_path):
    db = tmp_path / "events.db"
    assert reception_request(db, now=1000) is None
    set_listening_hold(db, 10, now=1000)
    assert reception_request(db, now=1005) == "listening"
    assert reception_request(db, now=1011) is None, "закрита вкладка не тримає приймач вимкненим"
    set_listening_hold(db, 10, now=2000)
    set_pause_minutes(db, 5, now=2000)
    assert reception_request(db, now=2001) == "pause"
    set_listening_hold(db, 0, now=2002)
    assert reception_request(db, now=2003) == "pause", "відпускання утримання не знімає паузу оператора"
    for bad in (-1, 121, float("nan"), "10"):
        with pytest.raises(ValueError):
            set_listening_hold(db, bad)


def test_concurrent_playback_updates_cannot_cancel_global_pause(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    db = tmp_path / 'events.db'
    set_pause_minutes(db, 10, now=1000)
    def playback():
        for i in range(30):
            set_listening_hold(db, 10 if i % 2 else 0, now=1000+i)
    def inspect():
        for _ in range(30):
            assert reception_request(db, now=1100) == 'pause'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(playback), pool.submit(inspect)]
        for future in futures:
            future.result()
    set_listening_hold(db, 0, now=1100)
    assert reception_request(db, now=1101) == 'pause'


def test_active_playback_remains_after_global_pause_expires(tmp_path):
    db = tmp_path / 'events.db'
    set_pause_minutes(db, 1, now=1000)
    set_listening_hold(db, 100, now=1050)
    assert reception_request(db, now=1059) == 'pause'
    assert reception_request(db, now=1061) == 'listening'
    set_listening_hold(db, 0, now=1062)
    assert reception_request(db, now=1063) is None


def run_supervisor(checks, segments):
    streams, states, log = [], [], Mock()

    class Stream:
        def __init__(self, **kwargs):
            streams.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Thread:
        def __init__(self, target, daemon):
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    class Segmenter:
        def __init__(self, **kwargs):
            pass

        def process(self, frame):
            return None

    class Frames:
        def __init__(self):
            self.calls = 0

        def get(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return b"frame"
            raise KeyboardInterrupt

        def get_nowait(self):
            raise queue.Empty

    gate = PauseGate()
    gate_calls = []
    original = gate.set_paused
    gate.set_paused = lambda value: (gate_calls.append(value), original(value))
    state = Mock()
    state.set.side_effect = states.append
    supervisor = ApplicationSupervisor(
        frames=Frames(), segments=segments, on_audio=Mock(), transcriber=Mock(), telegram_listener=None,
        sample_rate=16000, frame_ms=30, frame_len=480, vad_level=2, preroll_ms=500, silence_end_ms=400,
        min_speech_ms=500, max_segment_s=20, log=log, stream_factory=Stream, segmenter_factory=Segmenter,
        thread_factory=Thread, pause_checker=lambda: next(checks), pause_gate=gate, runtime_state=state)
    with patch("server.supervisor.time.sleep"):
        supervisor.run()
    return streams, states, gate_calls, log


def test_listening_hold_closes_microphone_but_keeps_recorded_phrases():
    segments = queue.Queue()
    segments.put(b"phrase recorded before playback")
    streams, states, gate_calls, log = run_supervisor(iter([False, False, "listening", "listening", False]), segments)
    assert len(streams) == 2, "мікрофон закрито й знову відкрито"
    assert states[:3] == ["listening", "paused", "listening"]
    assert segments.qsize() == 1, "записана до прослуховування фраза не відкидається"
    assert True not in gate_calls[:-1], "обробка вже записаних фраз не переривається"
    assert any("прослуховування" in str(call) for call in log.info.call_args_list)


def test_operator_pause_during_hold_becomes_full_pause():
    segments = queue.Queue()
    segments.put(b"phrase")
    streams, states, gate_calls, _ = run_supervisor(iter([False, False, "listening", "pause", False]), segments)
    assert segments.qsize() == 0, "звичайна пауза, як і раніше, очищає чергу"
    assert gate_calls[0] is False and True in gate_calls
