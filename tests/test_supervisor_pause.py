import queue
from unittest.mock import Mock, patch

from core.supervisor import ApplicationSupervisor
from core.pause_gate import PauseGate


def test_stream_closes_and_reopens_on_pause():
    streams = []
    states = []
    checks = iter([False, False, True, False])

    class Stream:
        def __init__(self, **kwargs):
            self.closed = False
            streams.append(self)

        def __enter__(self):
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

    state = Mock()
    state.set.side_effect = states.append

    supervisor = ApplicationSupervisor(
        frames=Frames(),
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
        log=Mock(),
        stream_factory=Stream,
        segmenter_factory=Segmenter,
        thread_factory=FakeThread,
        pause_checker=lambda: next(checks),
        pause_gate=PauseGate(),
        runtime_state=state,
    )

    with patch("core.supervisor.time.sleep"):
        supervisor.run()

    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert states == [
        "listening",
        "paused",
        "listening",
        "stopped",
    ]
