"""Сторож мікрофона, виявлення сну Mac і метрики коротких/тихих сегментів."""

import queue
from unittest.mock import Mock

import numpy as np

from core.diagnostics import runtime
from core.supervisor import ApplicationSupervisor
from core.transcriber import Transcriber
from core.vad import VADSegmenter


class Stream:
    opened = []

    def __init__(self, **kwargs):
        self.closed = False
        Stream.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Thread:
    def __init__(self, target, daemon):
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class Segmenter:
    created = 0

    def __init__(self, **kwargs):
        Segmenter.created += 1

    def process(self, frame):
        return None


class Frames:
    """Видає кадри за сценарієм; None — порожня черга; кінець — Ctrl+C."""

    def __init__(self, script, on_get=None):
        self.script = list(script)
        self.on_get = on_get

    def get(self, timeout=None):
        if self.on_get:
            self.on_get()
        if not self.script:
            raise KeyboardInterrupt
        item = self.script.pop(0)
        if item is None:
            raise queue.Empty
        return item

    def get_nowait(self):
        raise queue.Empty


def supervisor(frames, log, **kwargs):
    return ApplicationSupervisor(
        frames=frames, segments=queue.Queue(), on_audio=Mock(), transcriber=Mock(),
        telegram_listener=None, sample_rate=16000, frame_ms=30, frame_len=480,
        vad_level=2, preroll_ms=500, silence_end_ms=400, min_speech_ms=500,
        max_segment_s=20, log=log, stream_factory=Stream, segmenter_factory=Segmenter,
        thread_factory=Thread, **kwargs,
    )


def counter(name):
    return runtime.snapshot()["counters"].get(name, 0)


def test_silent_stream_is_reopened(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.supervisor.time.monotonic", lambda: clock[0])

    def tick():
        clock[0] += 4.0  # кожна порожня спроба — 4 с без кадрів

    Stream.opened = []
    before = counter("audio.stream_stalls")
    log = Mock()
    supervisor(Frames([b"frame", None, None, None, b"frame"], tick), log,
               suspend_threshold_s=float("inf")).run()

    assert len(Stream.opened) == 2, "потік перевідкрито після ~12 с тиші"
    assert Stream.opened[0].closed
    assert counter("audio.stream_stalls") == before + 1
    assert "перевідкриваю" in log.error.call_args.args[0]


def test_steady_frames_never_trigger_watchdog(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.supervisor.time.monotonic", lambda: clock[0])

    def tick():
        clock[0] += 5.0

    Stream.opened = []
    supervisor(Frames([b"frame"] * 10, tick), Mock(), suspend_threshold_s=float("inf")).run()
    assert len(Stream.opened) == 1


def test_mac_sleep_is_reported_and_unfinished_phrase_dropped():
    wall = [1000.0]

    def jump():
        wall[0] += 2 * 3600  # Mac спав дві години між ітераціями

    frames = Frames([b"frame", b"frame"])
    original_get = frames.get

    def get(timeout=None):
        item = original_get(timeout)
        jump()
        return item

    frames.get = get
    Segmenter.created = 0
    before = counter("receiver.suspensions")
    log = Mock()
    supervisor(frames, log, stall_timeout_s=float("inf"), wall_clock=lambda: wall[0]).run()

    assert counter("receiver.suspensions") > before
    message = log.warning.call_args.args
    assert "сон Mac" in message[0] and message[1] == 120
    assert Segmenter.created >= 2, "VAD скинуто після сну"


def test_short_vad_fragment_is_counted():
    vad = VADSegmenter(sample_rate=16000, frame_ms=30, vad_level=0, preroll_ms=90,
                       silence_end_ms=90, min_speech_ms=500, max_segment_s=20)
    speech = [True, True, False, False, False]
    vad.vad = Mock(is_speech=Mock(side_effect=speech))
    before = counter("vad.short_discarded")
    assert all(vad.process(b"\0" * 960) is None for _ in speech)
    assert counter("vad.short_discarded") == before + 1
    assert runtime.snapshot()["timings"]["vad.short_discarded_speech"]["max_s"] >= 0.06


def test_rms_levels_are_recorded_without_text():
    worker = Transcriber(
        segments=None, recognizer=Mock(), process_text=Mock(), event_processor=Mock(),
        sample_rate=16000, min_rms=0.02, normalize_audio=True, debug_rms=False,
        blacklist=(), log=Mock(),
    )
    worker.recognizer.extract_text.return_value = ""
    quiet = (np.ones(16000) * 100).astype(np.int16).tobytes()
    loud = (np.ones(16000) * 3000).astype(np.int16).tobytes()

    worker.process_segment(quiet)
    worker.process_segment(loud)

    timings = runtime.snapshot()["timings"]
    assert timings["asr.low_rms_level"]["max_s"] < 0.02
    assert timings["asr.accepted_rms_level"]["max_s"] > 0.02
    worker.recognizer.transcribe.assert_called_once()


def sleeping_supervisor(on_suspension, log=None):
    wall = [1000.0]
    frames = Frames([b"frame", b"frame"])
    original_get = frames.get

    def get(timeout=None):
        item = original_get(timeout)
        wall[0] += 3 * 3600 + 25 * 60
        return item

    frames.get = get
    return supervisor(frames, log or Mock(), stall_timeout_s=float("inf"),
                      wall_clock=lambda: wall[0], on_suspension=on_suspension)


def test_suspension_triggers_admin_notification():
    calls = []
    sleeping_supervisor(calls.append).run()
    assert calls and calls[0] >= 3 * 3600


def test_failing_notification_never_stops_reception():
    log = Mock()
    Stream.opened = []
    sleeping_supervisor(Mock(side_effect=RuntimeError("мережа")), log).run()
    log.exception.assert_called()
    assert Stream.opened[0].closed, "цикл завершився штатно"


def test_admin_alert_retries_until_delivered(monkeypatch):
    import logging
    import threading

    from services.telegram import TelegramService

    monkeypatch.setattr("services.telegram.time.sleep", Mock())
    service = TelegramService(
        bot_token="t", chat_id="1001", sample_rate=16000, db_path=":memory:",
        log=logging.getLogger("test"), state_lock=threading.Lock(),
        get_notify_mode=lambda: "all", set_notify_mode=lambda m: None,
    )
    import requests
    service._request = Mock(side_effect=[requests.ConnectionError(), Mock(status_code=502), Mock(status_code=200)])
    before = counter("telegram.alert_sent")
    service.notify_admin_async("⚠️ test").join(timeout=5)
    assert service._request.call_count == 3
    assert service._request.call_args.kwargs["data"]["chat_id"] == "1001"
    assert counter("telegram.alert_sent") == before + 1
