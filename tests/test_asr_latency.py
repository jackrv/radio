"""Затримки на штучному годиннику; моделі, мікрофон і мережа не запускаються."""

import json
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from server.bounded_queue import DropOldestQueue
from shared.diagnostics import RuntimeDiagnostics
from server.pause_gate import PauseGate
from shared.pipeline import build_text_pipeline
from server.segment_timing import CapturedFrame, SegmentTiming, TimedSegment, capture_frame
from server.transcriber import Transcriber


class Clock:
    value = 13.0

    def now(self):
        return self.value

    def advance(self, value):
        self.value += value


def pcm():
    return np.full(32000, 10000, dtype="<i2").tobytes()


def timed_pcm():
    return TimedSegment(pcm(), SegmentTiming(
        enqueued_at=11, enqueued_unix_s=1011,
        audio_end_at=10.6, audio_end_unix_s=1010.6,
        speech_end_at=10, speech_end_unix_s=1010, capture_source="portaudio",
    ))


def worker(monkeypatch, *, qwen=True, gate=None):
    clock = Clock()
    metrics = RuntimeDiagnostics(clock=clock.now, wall_clock=lambda: 1000 + clock.value)
    monkeypatch.setattr("server.transcriber.runtime", metrics)
    monkeypatch.setattr("shared.pipeline.runtime", metrics)
    monkeypatch.setattr("server.transcriber.looks_like_garbage", lambda *_: False)

    def whisper(_):
        clock.advance(1.5)
        return {}

    def correct(text):
        clock.advance(0.7)
        return text

    def learned(text):
        clock.advance(0.1)
        return text, False

    def guard(*_):
        clock.advance(0.2)
        return True

    pipeline = build_text_pipeline(
        correct_with_llm=correct if qwen else None, apply_learned_corrections=learned,
        dictionaries=(), correction_is_safe=lambda *_: True, log=Mock(), llm_guard=guard,
    )
    recognizer = Mock(transcribe=Mock(side_effect=whisper))
    recognizer.extract_text.return_value = "ПРИВАТНИЙ_ТЕКСТ_НЕ_ДЛЯ_JSON"
    processor = Mock()
    processor.process.side_effect = lambda *_, **kw: clock.advance(4)
    transcriber = Transcriber(
        segments=queue.Queue(), recognizer=recognizer, process_text=pipeline,
        event_processor=processor, sample_rate=16000, min_rms=0.02,
        normalize_audio=True, debug_rms=False, blacklist=(), log=Mock(), pause_gate=gate,
    )
    return transcriber, metrics, clock


def record(metrics):
    return metrics.snapshot()["asr_latency"]["recent_segments"][-1]


def test_audio_end_to_ready_is_paired_and_excludes_event_delivery(monkeypatch, tmp_path):
    transcriber, metrics, _ = worker(monkeypatch)
    transcriber.process_segment(timed_pcm())
    row = record(metrics)
    expected = {"audio_duration_s": 2, "queue_wait_s": 2, "vad_release_s": 1,
                "speech_tail_s": 0.6, "capture_to_enqueue_s": 0.4,
                "whisper_s": 1.5, "qwen_s": 0.7, "correction_s": 1.1,
                "processing_to_text_s": 2.6, "total_latency_s": 5.6,
                "segment_to_text_s": 4.6, "event_s": 4, "worker_busy_s": 6.6,
                "rtf": 1.3, "worker_rtf": 3.3}
    for field, value in expected.items():
        assert row[field] == pytest.approx(value), field
    assert row["whisper_calls"] == row["qwen_calls"] == 1
    assert row["status"] == "ready"
    assert row["speech_end_unix_s"] == 1010
    assert row["text_ready_unix_s"] == pytest.approx(1015.6)
    assert row["total_latency_s"] == pytest.approx(row["vad_release_s"] + row["queue_wait_s"] + row["processing_to_text_s"])
    path = tmp_path / "diagnostics.json"
    metrics.configure(path)
    saved = path.read_text()
    assert "ПРИВАТНИЙ" not in saved and "raw_text" not in saved and '"pcm"' not in saved
    assert json.loads(saved)["asr_latency"]["ready"]["rtf"]["unit"] == "ratio"


def test_no_qwen_call_is_explicitly_zero_for_the_same_segment(monkeypatch):
    transcriber, metrics, _ = worker(monkeypatch, qwen=False)
    transcriber.process_segment(timed_pcm())
    row = record(metrics)
    assert row["qwen_calls"] == 0 and row["qwen_s"] == 0
    assert row["processing_to_text_s"] == pytest.approx(1.6)


def test_correction_error_measures_fallback_without_leaking_exception(monkeypatch):
    transcriber, metrics, clock = worker(monkeypatch)

    def fail(_):
        with metrics.timed("asr.qwen"):
            clock.advance(2)
            raise RuntimeError("ПРИВАТНА_ПОМИЛКА")

    transcriber.process_text = fail
    transcriber.process_segment(timed_pcm())
    row = record(metrics)
    assert row["status"] == "ready_fallback"
    assert row["qwen_s"] == 2 and row["whisper_s"] == 1.5
    assert row["total_latency_s"] == 6.5
    assert "ПРИВАТНА" not in json.dumps(metrics.snapshot(), ensure_ascii=False)


def test_whisper_error_and_low_rms_do_not_bias_ready_latency(monkeypatch):
    transcriber, metrics, _ = worker(monkeypatch)
    transcriber.recognizer.transcribe.side_effect = RuntimeError("private")
    transcriber.process_segment(timed_pcm())
    assert record(metrics)["status"] == "whisper_error"
    transcriber.process_segment(bytes(32000))
    assert record(metrics)["status"] == "low_rms"
    assert metrics.snapshot()["asr_latency"]["ready"] == {}


@pytest.mark.parametrize("stage", ["whisper", "qwen"])
def test_paused_inflight_segment_never_gets_ready_latency(monkeypatch, stage):
    gate = PauseGate()
    transcriber, metrics, clock = worker(monkeypatch, gate=gate)
    generation = gate.tag()
    if stage == "whisper":
        transcriber.recognizer.transcribe.side_effect = lambda _: gate.set_paused(True)
    else:
        def pause(_):
            with metrics.timed("asr.qwen"):
                clock.advance(1)
                gate.set_paused(True)
            return "private"
        transcriber.process_text = pause
    transcriber.process_segment(timed_pcm(), generation=generation)
    assert record(metrics)["status"] == "stale"
    assert "total_latency_s" not in record(metrics)
    assert metrics.snapshot()["asr_latency"]["ready"] == {}
    transcriber.event_processor.process.assert_not_called()


@pytest.mark.parametrize("gated", [False, True])
def test_legacy_queue_shapes_still_work_without_fabricated_end_times(monkeypatch, gated):
    gate = PauseGate() if gated else None
    transcriber, metrics, _ = worker(monkeypatch, gate=gate)
    transcriber.segments = Mock()
    item = (gate.tag(), pcm()) if gated else pcm()
    transcriber.segments.get.side_effect = [item, KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        transcriber.run()
    row = record(metrics)
    assert row["status"] == "ready" and row["capture_source"] == "legacy"
    assert "total_latency_s" not in row and "queue_wait_s" not in row
    assert metrics.snapshot()["counters"]["asr.timing_missing"] == 1


def test_stale_timed_queue_item_is_recorded_but_never_recognized(monkeypatch):
    gate = PauseGate()
    transcriber, metrics, _ = worker(monkeypatch, gate=gate)
    old = gate.tag()
    gate.set_paused(True)
    gate.set_paused(False)
    transcriber.segments = Mock()
    transcriber.segments.get.side_effect = [(old, timed_pcm()), KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        transcriber.run()
    assert record(metrics)["status"] == "stale"
    transcriber.recognizer.transcribe.assert_not_called()


def test_queue_peak_and_drops_survive_an_empty_snapshot():
    metrics = RuntimeDiagnostics()
    segments = DropOldestQueue(2)
    for index in range(3):
        segments.put(index)
    segments.get()
    segments.get()
    metrics.observe_asr_queue(segments)
    gauges = metrics.snapshot()["gauges"]
    assert gauges["audio.segments_queue"] == 0
    assert gauges["audio.segments_dropped"] == 1
    assert gauges["asr.segment_queue_peak"] == 2


def test_recent_records_and_p95_are_bounded_while_mean_and_max_cover_session():
    clock = Clock()
    metrics = RuntimeDiagnostics(clock=clock.now)
    for index in range(1, 202):
        with metrics.asr_segment(pcm(), 16000, queue.Queue()):
            with metrics.timed("asr.whisper"):
                clock.advance(index / 1000)
            metrics.asr_text_ready()
    snapshot = metrics.snapshot()["asr_latency"]
    assert len(snapshot["recent_segments"]) == 128
    stats = snapshot["ready"]["processing_to_text_s"]
    assert stats["count"] == 201
    assert stats["mean"] == pytest.approx(0.101)
    assert stats["max"] == pytest.approx(0.201)
    assert stats["recent_p95"] == pytest.approx(0.195)


def test_wall_clock_jump_does_not_change_elapsed_latency(monkeypatch):
    transcriber, metrics, _ = worker(monkeypatch)
    metrics._wall_clock = lambda: -1000
    transcriber.process_segment(timed_pcm())
    assert record(metrics)["total_latency_s"] == pytest.approx(5.6)


def test_future_metadata_does_not_fabricate_negative_waiting_times(monkeypatch):
    transcriber, metrics, _ = worker(monkeypatch)
    packet = TimedSegment(pcm(), SegmentTiming(enqueued_at=9999, enqueued_unix_s=9999))
    transcriber.process_segment(packet)
    row = record(metrics)
    assert row["status"] == "ready"
    assert "queue_wait_s" not in row and "total_latency_s" not in row
    assert metrics.snapshot()["counters"]["asr.timing_missing"] == 1


def test_parallel_measurements_do_not_mix_qwen_and_whisper_stages():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    metrics = RuntimeDiagnostics()
    barrier = Barrier(2)

    def measure(stage):
        with metrics.asr_segment(pcm(), 16000, queue.Queue()):
            with metrics.timed(stage):
                barrier.wait(timeout=5)
            metrics.asr_text_ready()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(measure, stage) for stage in ("asr.whisper", "asr.qwen")]
        for future in futures:
            future.result(timeout=5)
    rows = metrics.snapshot()["asr_latency"]["recent_segments"]
    assert {(r["whisper_calls"], r["qwen_calls"]) for r in rows} == {(1, 0), (0, 1)}


def test_capture_timestamp_uses_adc_end_and_falls_back_safely(monkeypatch):
    monkeypatch.setattr("server.segment_timing.time.perf_counter", lambda: 100)
    monkeypatch.setattr("server.segment_timing.time.time", lambda: 1000)
    frame = capture_frame(b"S", 480, 16000, SimpleNamespace(currentTime=40, inputBufferAdcTime=39.95))
    assert frame == b"S" and isinstance(frame, bytes)
    assert frame.capture_source == "portaudio"
    assert frame.captured_at == pytest.approx(99.98)
    assert frame.captured_unix_s == pytest.approx(999.98)
    for info in (None, SimpleNamespace(currentTime=float("nan"), inputBufferAdcTime=0)):
        fallback = capture_frame(b"S", 480, 16000, info)
        assert fallback.capture_source == "callback" and fallback.captured_at == 100


@pytest.mark.parametrize("gated", [False, True])
def test_supervisor_transports_speech_end_without_changing_pcm_or_generation(monkeypatch, gated):
    from tests.test_supervisor import make_supervisor
    from tests.test_vad import make_segmenter

    metrics = RuntimeDiagnostics()
    monkeypatch.setattr("server.supervisor.runtime", metrics)
    frames = [CapturedFrame(value, captured_at=end, captured_unix_s=1000 + end, source="portaudio")
              for value, end in ((b"S", 10), (b"X", 10.03), (b"X", 10.06))]
    supervisor, segments, _ = make_supervisor(queue.Queue())
    supervisor.frames.get = Mock(side_effect=frames + [KeyboardInterrupt()])
    supervisor.segmenter_factory = lambda **_: make_segmenter()
    if gated:
        supervisor.pause_gate = PauseGate()
        expected_generation = supervisor.pause_gate.tag()
    supervisor.run()
    item = segments.get_nowait()
    if gated:
        generation, item = item
        assert generation == expected_generation
        assert supervisor.pause_gate.tag() is None, "штатна зупинка залишає приймач на паузі"
    assert isinstance(item, bytes) and item == b"SXX"
    assert item.timing.speech_end_at == 10
    assert item.timing.audio_end_at == 10.06
    assert item.timing.speech_end_unix_s == 1010
    assert metrics.snapshot()["counters"]["asr.enqueued"] == 1
