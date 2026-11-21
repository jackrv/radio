import requests
"""Сторож мікрофона, виявлення сну Mac і метрики коротких/тихих сегментів."""

import queue
from unittest.mock import Mock

import numpy as np
import pytest

from core.diagnostics import runtime
from core.pause_gate import PauseGate
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
    assert "активного приймання" in message[0] and message[1] == 120
    assert "Причину не встановлено" in message[0]
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


@pytest.mark.parametrize("reason", ["pause", "listening"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("jump_clock", ["wall", "monotonic", "both"])
def test_gap_while_microphone_is_intentionally_closed_never_alerts(
    monkeypatch, reason, resume, jump_clock,
):
    wall, mono, requested = [1000.0], [100.0], [reason]
    monkeypatch.setattr("core.supervisor.time.monotonic", lambda: mono[0])
    sleeps = []
    segments = queue.Queue()
    segments.put(b"completed phrase before pause")

    def sleep(_):
        if sleeps:
            raise KeyboardInterrupt
        sleeps.append(True)
        if jump_clock in ("wall", "both"):
            wall[0] += 180
        if jump_clock in ("monotonic", "both"):
            mono[0] += 180
        if resume:
            requested[0] = False  # строк паузи/hold сплив під час розриву

    monkeypatch.setattr("core.supervisor.time.sleep", sleep)
    before = counter("receiver.suspensions")
    state_before = counter(f"receiver.loop_gaps.{reason}")
    alerts, log, gate = Mock(), Mock(), PauseGate()
    Stream.opened = []
    sup = supervisor(Frames([b"frame"]), log, pause_checker=lambda: requested[0],
                     pause_gate=gate, wall_clock=lambda: wall[0], on_suspension=alerts)
    sup.segments = segments
    sup.run()

    alerts.assert_not_called()
    assert counter("receiver.suspensions") == before
    assert counter(f"receiver.loop_gaps.{reason}") == state_before + 1
    snapshot = runtime.snapshot()
    assert snapshot["gauges"]["receiver.last_loop_gap_state"] == reason
    assert snapshot["timings"][f"receiver.loop_gap.{reason}"]["max_s"] >= 180
    assert len(Stream.opened) == int(resume)
    assert all(stream.closed for stream in Stream.opened)
    # Прослуховування не інвалідує завершені фрази; ручна пауза їх очищає.
    assert segments.qsize() == int(reason == "listening")
    log.warning.assert_not_called()


def test_real_gap_after_resuming_is_not_suppressed_by_the_previous_pause(monkeypatch):
    wall, requested, frames_seen = [1000.0], [False], []

    def frame():
        frames_seen.append(True)
        if len(frames_seen) == 1:
            requested[0] = "pause"
        elif len(frames_seen) == 2:
            wall[0] += 120  # справжній розрив після перевідкриття потоку

    def sleep(_):
        wall[0] += 180
        requested[0] = False

    monkeypatch.setattr("core.supervisor.time.sleep", sleep)
    alerts = Mock()
    pause_before, active_before = counter("receiver.loop_gaps.pause"), counter("receiver.suspensions")
    Stream.opened = []
    supervisor(Frames([b"frame", b"frame"], frame), Mock(), pause_checker=lambda: requested[0],
               wall_clock=lambda: wall[0], on_suspension=alerts).run()
    alerts.assert_called_once_with(120)
    assert counter("receiver.loop_gaps.pause") == pause_before + 1
    assert counter("receiver.suspensions") == active_before + 1
    assert len(Stream.opened) == 2 and all(stream.closed for stream in Stream.opened)


def test_new_pause_request_does_not_hide_a_gap_that_started_while_stream_was_open(monkeypatch):
    wall, requested = [1000.0], [False]

    def frame():
        wall[0] += 180
        requested[0] = "pause"  # намір новий; під час розриву мікрофон ще був відкритий

    monkeypatch.setattr("core.supervisor.time.sleep", Mock(side_effect=KeyboardInterrupt))
    alerts = Mock()
    supervisor(Frames([b"frame"], frame), Mock(), pause_checker=lambda: requested[0],
               wall_clock=lambda: wall[0], on_suspension=alerts).run()
    alerts.assert_called_once_with(180)


def test_unavailable_microphone_is_not_mislabelled_as_an_intentional_pause(monkeypatch):
    wall, mono, sleeps = [1000.0], [100.0], []
    monkeypatch.setattr("core.supervisor.time.monotonic", lambda: mono[0])

    def sleep(_):
        if sleeps:
            raise KeyboardInterrupt
        sleeps.append(True)
        wall[0] += 180
        mono[0] += 180

    monkeypatch.setattr("core.supervisor.time.sleep", sleep)
    alerts, log = Mock(), Mock()
    factory = Mock(side_effect=RuntimeError("synthetic device error"))
    before = counter("receiver.loop_gaps.unavailable")
    sup = supervisor(Frames([]), log, wall_clock=lambda: wall[0], on_suspension=alerts)
    sup.stream_factory = factory
    sup.run()
    alerts.assert_not_called()
    assert counter("receiver.loop_gaps.unavailable") == before + 1
    assert factory.call_count == 2, "помилки мікрофона й штатні повтори не вимкнено"
    assert log.exception.call_count == 2


def test_long_intentional_pause_with_a_responsive_loop_is_not_a_loop_gap(monkeypatch):
    clock, sleeps = [1000.0], []
    monkeypatch.setattr("core.supervisor.time.monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        sleeps.append(True)
        if len(sleeps) >= 450:
            raise KeyboardInterrupt

    monkeypatch.setattr("core.supervisor.time.sleep", sleep)
    before = counter("receiver.loop_gaps")
    alerts = Mock()
    supervisor(Frames([]), Mock(), pause_checker=lambda: "pause",
               wall_clock=lambda: clock[0], on_suspension=alerts).run()
    assert counter("receiver.loop_gaps") == before
    alerts.assert_not_called()


def test_global_pause_with_playback_and_release_never_alerts_or_resumes_early(tmp_path, monkeypatch):
    from services.receiver_control import reception_request, set_listening_hold, set_pause_minutes

    db_path = tmp_path / "events.db"
    wall, sleeps = [1000.0], []
    set_pause_minutes(db_path, 5, now=wall[0])
    set_listening_hold(db_path, 120, now=wall[0])
    alerts = Mock()
    Stream.opened = []

    def sleep(_):
        sleeps.append(True)
        assert not Stream.opened
        if len(sleeps) == 1:
            wall[0] += 180
            set_listening_hold(db_path, 0, now=wall[0])
            assert reception_request(db_path, now=wall[0]) == "pause"
        elif len(sleeps) == 2:
            wall[0] += 180
            set_pause_minutes(db_path, 0, now=wall[0])
        else:
            raise AssertionError("приймач не відновився")

    monkeypatch.setattr("core.supervisor.time.sleep", sleep)
    supervisor(Frames([b"frame"]), Mock(), pause_checker=lambda: reception_request(db_path, now=wall[0]),
               wall_clock=lambda: wall[0], on_suspension=alerts).run()
    alerts.assert_not_called()
    assert len(Stream.opened) == 1 and Stream.opened[0].closed


def test_gap_notification_does_not_assert_sleep_or_successful_recovery():
    # Виконуємо тільки чисту функцію форматування, не імпортуємо запуск radio.py.
    import ast
    from datetime import datetime, timedelta
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "radio.py").read_text())
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "report_suspension")
    telegram = Mock()
    namespace = {"datetime": datetime, "timedelta": timedelta, "telegram_service": telegram}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "radio.py", "exec"), namespace)
    namespace["report_suspension"](185)
    message = telegram.notify_admin_async.call_args.args[0]
    assert "Розрив циклу під час приймання" in message
    assert "Причину не встановлено" in message
    assert "закрита кришка" not in message
    assert "відновлено автоматично" not in message


def test_admin_alert_retries_until_delivered(monkeypatch):
    import logging

    from services.telegram import TelegramService

    monkeypatch.setattr("time.sleep", Mock())
    service = TelegramService(
        bot_token="t",
        sample_rate=16000,
        db_path=":memory:",
        log=logging.getLogger("test"),
    )
    service._admin_chats = Mock(return_value=[1001])
    service._user_state = Mock(return_value=("admin", "all"))
    import requests
    service._request = Mock(side_effect=[requests.ConnectionError(), Mock(status_code=502), Mock(status_code=200)])
    before = counter("telegram.alert_sent")
    service.notify_admin_async("⚠️ test").join(timeout=5)
    assert service._request.call_count == 3
    assert service._request.call_args.kwargs["data"]["chat_id"] == 1001
    assert counter("telegram.alert_sent") == before + 1


# ---------- Прогрес розпізнавання ----------

from core.recognition_health import RecognitionHealth


def test_stuck_segment_is_detected():
    clock = [0.0]
    health = RecognitionHealth(clock=lambda: clock[0])
    health.started()
    clock[0] = 179.0
    assert health.problem() is None
    clock[0] = 181.0
    assert health.problem()[0] == "stuck"
    health.finished(True)
    assert health.problem() is None


def test_error_streak_counts_only_whisper_calls():
    health = RecognitionHealth()
    for _ in range(4):
        health.finished(False)
        health.finished(None)  # тихий сегмент без Whisper не скидає серію
    assert health.problem() is None
    health.finished(False)
    assert health.problem() == ("failing", 5)
    health.finished(True)
    assert health.problem() is None


def test_transcriber_reports_whisper_failures_and_filtered_segments():
    health = RecognitionHealth()
    loud = (np.ones(16000) * 3000).astype(np.int16).tobytes()
    quiet = (np.ones(16000) * 10).astype(np.int16).tobytes()
    recognizer = Mock()
    recognizer.transcribe.side_effect = RuntimeError("Metal OOM")
    worker = Transcriber(
        segments=None, recognizer=recognizer, process_text=Mock(), event_processor=Mock(),
        sample_rate=16000, min_rms=0.02, normalize_audio=True, debug_rms=False,
        blacklist=(), log=Mock(), health=health,
    )
    for _ in range(5):
        worker._tracked(loud)
        worker._tracked(quiet)
    assert health.problem() == ("failing", 5)


def test_supervisor_alerts_once_and_reports_recovery():
    state = {"problem": None}
    health = Mock(problem=lambda: state["problem"])
    calls = []
    log = Mock()
    sup = supervisor(Frames([]), log, stall_timeout_s=float("inf"), suspend_threshold_s=float("inf"),
                     recognition_health=health, on_recognition_problem=lambda k, v: calls.append(k))
    reported = None
    for problem in [None, ("stuck", 200.0), ("stuck", 260.0), None, None]:
        state["problem"] = problem
        reported = sup._check_recognition(reported)
    assert calls == ["stuck", "recovered"]
    log.critical.assert_called_once()
