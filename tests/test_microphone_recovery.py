import queue
from unittest.mock import Mock

from core.pause_gate import PauseGate
from core.supervisor import ApplicationSupervisor


def test_microphone_recovers_after_open_failure(monkeypatch):
    clock = [100.0]
    streams = []
    states = []
    requests = iter([False, True, False, False, False])

    monkeypatch.setattr(
        "core.supervisor.time.monotonic",
        lambda: clock[0],
    )

    def fake_sleep(seconds):
        clock[0] += 5.0

    monkeypatch.setattr(
        "core.supervisor.time.sleep",
        fake_sleep,
    )

    class FakeStream:
        def __init__(self, **kwargs):
            self.number = len(streams) + 1
            self.closed = False
            streams.append(self)

        def __enter__(self):
            if self.number == 2:
                raise RuntimeError("Input device unavailable")
            return self

        def __exit__(self, *args):
            self.closed = True

    class FakeThread:
        def __init__(self, target, daemon):
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    class FakeSegmenter:
        def __init__(self, **kwargs):
            pass

        def process(self, frame):
            return None

    class FakeFrames:
        def __init__(self):
            self.calls = 0

        def get(self, timeout=None):
            self.calls += 1

            if self.calls == 1:
                return b"frame"

            raise KeyboardInterrupt

        def get_nowait(self):
            raise queue.Empty

    runtime = Mock()
    runtime.set.side_effect = states.append
    log = Mock()
    gate = PauseGate()

    supervisor = ApplicationSupervisor(
        frames=FakeFrames(),
        segments=queue.Queue(),
        on_audio=Mock(),
        transcriber=Mock(),
        telegram_listener=None,
        sample_rate=16000,
        frame_ms=30,
        frame_len=480,
        vad_level=2,
        preroll_ms=500,
        silence_end_ms=400,
        min_speech_ms=500,
        max_segment_s=20,
        log=log,
        stream_factory=FakeStream,
        segmenter_factory=FakeSegmenter,
        thread_factory=FakeThread,
        pause_checker=lambda: next(requests),
        pause_gate=gate,
        runtime_state=runtime,
    )

    supervisor.run()

    # Первый поток открылся.
    # Второй не открылся.
    # Третий восстановил приём.
    assert len(streams) == 3

    assert streams[0].closed
    assert streams[2].closed

    assert states == [
        "listening",
        "paused",
        "stopped",
        "listening",
        "stopped",
    ]

    log.exception.assert_called_once()
    assert gate.is_current(gate.tag()) is False
