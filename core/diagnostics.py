"""Bounded, content-free runtime measurements. Never changes retry decisions."""

import errno
import logging
import math
from pathlib import Path
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar

from services.diagnostic_history import (
    DiagnosticsHistory, atomic_write, history_directory,
    DEFAULT_RETENTION_DAYS, DEFAULT_MAX_SESSIONS, DEFAULT_MAX_BYTES,
)

_asr_span = ContextVar("radio345_asr_span", default=None)
_ASR_STAGES = {
    "asr.whisper": "whisper_s", "asr.qwen": "qwen_s",
    "asr.correction_pipeline": "correction_s", "asr.event_processing": "event_s",
    "asr.notification_filter": "notification_filter_s",
    "asr.notification_spectral": "notification_spectral_s",
}
_SPECTRAL_FIELDS = ("notification_score", "notification_outside_s", "notification_extra", "notification_candidate")
# Сегменти з такою або вищою спектральною оцінкою зберігаються для локальної панелі.
SPECTRAL_KEEP_SCORE = 0.5
SPECTRAL_BUCKETS = 20
_ASR_STATUSES = frozenset({
    "discarded", "ready", "ready_fallback", "stale", "low_rms", "empty_text",
    "garbage", "blacklist", "low_confidence", "notification", "whisper_error", "error",
})
_ASR_READY_METRICS = (
    "audio_duration_s", "queue_wait_s", "vad_release_s", "speech_tail_s", "capture_to_enqueue_s", "whisper_s", "qwen_s",
    "correction_s", "notification_filter_s", "processing_to_text_s", "total_latency_s",
    "segment_to_text_s", "rtf",
)
_ASR_ALL_METRICS = ("audio_duration_s", "queue_wait_s", "worker_busy_s", "worker_rtf", "event_s")
_ASR_RECORD_FIELDS = frozenset(_ASR_READY_METRICS + _ASR_ALL_METRICS + (
    "segment_id", "enqueued_unix_s", "audio_end_unix_s", "speech_end_unix_s",
    "processing_started_unix_s", "text_ready_unix_s", "queue_depth", "segments_dropped",
    "whisper_calls", "qwen_calls", "status", "capture_source", "notification_spectral_s",
) + _SPECTRAL_FIELDS)


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def error_kind(error):
    """Inspect exception structure, never record its message or request URL."""
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ConnectionResetError) or getattr(item, "errno", None) in (54, errno.ECONNRESET):
            return "connection_reset"
        if isinstance(item, BaseException):
            pending.extend(x for x in item.args if isinstance(x, BaseException))
            pending.extend(x for x in (item.__cause__, item.__context__, getattr(item, "reason", None)) if x is not None)
    name = type(error).__name__
    return name if name in ("ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError", "HTTPError") else "other"


class RuntimeDiagnostics:
    def __init__(self, clock=None, wall_clock=None):
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.session = uuid.uuid4().hex
        self.started_at = time.time()
        self.counters = Counter()
        self.timings = {}
        self.gauges = {}
        self.path = None
        self.history = None
        self._previous_pending = False
        self._ended_at = None
        self.last_flush = float("-inf")
        self._clock = clock
        self._wall_clock = wall_clock
        self._asr_sequence = 0
        self._asr_recent = deque(maxlen=128)
        self._asr_stats = {"ready": {}, "all_segments": {}}
        # Спектральний фільтр (спостереження): гістограма оцінок і останні високі збіги.
        self._spectral_buckets = [0] * SPECTRAL_BUCKETS
        self._spectral_recent = deque(maxlen=128)

    def _now(self):
        return self._clock() if self._clock is not None else time.perf_counter()

    def _wall(self):
        return self._wall_clock() if self._wall_clock is not None else time.time()

    def observe_asr_queue(self, queue):
        depth = queue.qsize() if hasattr(queue, "qsize") else None
        dropped = getattr(queue, "dropped", None)
        peak = getattr(queue, "high_watermark", depth)
        with self.lock:
            if type(depth) is int:
                self.gauges["audio.segments_queue"] = depth
            if type(dropped) is int:
                self.gauges["audio.segments_dropped"] = dropped
            if type(peak) is int:
                self.gauges["asr.segment_queue_peak"] = max(self.gauges.get("asr.segment_queue_peak", 0), peak)

    def _current_asr(self):
        active = _asr_span.get()
        return active[1] if active is not None and active[0] is self else None

    def asr_status(self, status):
        span = self._current_asr()
        if span is not None and status in _ASR_STATUSES:
            span["status"] = status

    def asr_notification_score(self, result):
        """Лише числа: оцінка, тривалість поза збігом, частка сторонньої енергії, кандидат."""
        span = self._current_asr()
        if span is None or not isinstance(result, dict):
            return
        values = (result.get("score"), result.get("outside_s"), result.get("extra_ratio"))
        if not all(_finite(v) for v in values):
            return
        span["notification_score"], span["notification_outside_s"], span["notification_extra"] = (float(v) for v in values)
        span["notification_candidate"] = bool(result.get("candidate"))

    def asr_text_ready(self, *, fallback=False):
        span = self._current_asr()
        if span is None:
            return
        ready = self._now()
        span["status"] = "ready_fallback" if fallback else "ready"
        span["text_ready_unix_s"] = self._wall()
        span["processing_to_text_s"] = max(0.0, ready - span["_started_at"])
        for origin, field in (("_speech_end_at", "total_latency_s"), ("_enqueued_at", "segment_to_text_s")):
            if _finite(span.get(origin)) and span[origin] <= ready:
                span[field] = ready - span[origin]
        if span["audio_duration_s"] > 0:
            span["rtf"] = span["processing_to_text_s"] / span["audio_duration_s"]

    @contextmanager
    def asr_segment(self, pcm, sample_rate, queue):
        started = self._now()
        self.observe_asr_queue(queue)
        with self.lock:
            self._asr_sequence += 1
            sequence = self._asr_sequence
            depth = self.gauges.get("audio.segments_queue", 0)
            dropped = self.gauges.get("audio.segments_dropped", 0)
        span = {"segment_id": sequence, "_started_at": started,
                "processing_started_unix_s": self._wall(), "status": "discarded",
                "audio_duration_s": len(pcm) / (2 * sample_rate),
                "queue_depth": depth, "segments_dropped": dropped,
                "whisper_s": 0.0, "qwen_s": 0.0, "correction_s": 0.0,
                "notification_filter_s": 0.0, "notification_spectral_s": 0.0, "event_s": 0.0,
                "whisper_calls": 0, "qwen_calls": 0, "capture_source": "legacy"}
        timing = getattr(pcm, "timing", None)
        if timing is not None:
            enqueued = getattr(timing, "enqueued_at", None)
            if _finite(enqueued) and enqueued <= started:
                span["_enqueued_at"] = enqueued
                span["queue_wait_s"] = started - enqueued
                speech_end = getattr(timing, "speech_end_at", None)
                audio_end = getattr(timing, "audio_end_at", None)
                if (_finite(speech_end) and _finite(audio_end)
                        and speech_end <= audio_end <= enqueued):
                    span["_speech_end_at"] = speech_end
                    span["vad_release_s"] = enqueued - speech_end
                    span["speech_tail_s"] = audio_end - speech_end
                    span["capture_to_enqueue_s"] = enqueued - audio_end
            for field in ("audio_end_unix_s", "speech_end_unix_s", "enqueued_unix_s"):
                value = getattr(timing, field, None)
                if _finite(value):
                    span[field] = value
            source = getattr(timing, "capture_source", None)
            if source in ("portaudio", "callback"):
                span["capture_source"] = source
        token = _asr_span.set((self, span))
        try:
            yield span
        except BaseException:
            span["status"] = "error"
            raise
        finally:
            _asr_span.reset(token)
            span["worker_busy_s"] = max(0.0, self._now() - started)
            if span["audio_duration_s"] > 0:
                span["worker_rtf"] = span["worker_busy_s"] / span["audio_duration_s"]
            # Явний білий список: PCM, текст, назви шаблонів і довільні об’єкти сюди не потраплять.
            record = {k: v for k, v in span.items() if k in _ASR_RECORD_FIELDS}
            with self.lock:
                self._asr_recent.append(record)
                self.counters["asr.measured_segments"] += 1
                self.counters[f"asr.outcome.{span['status']}"] += 1
                if "queue_wait_s" not in span:
                    self.counters["asr.timing_missing"] += 1
                if "notification_score" in span:
                    score = span["notification_score"]
                    self.counters["asr.spectral.scored"] += 1
                    bucket = min(SPECTRAL_BUCKETS - 1, max(0, int(score * SPECTRAL_BUCKETS)))
                    self._spectral_buckets[bucket] += 1
                    if span["notification_candidate"]:
                        self.counters["asr.spectral.candidate"] += 1
                        self.counters[f"asr.spectral.candidate.{span['status']}"] += 1
                    if score >= SPECTRAL_KEEP_SCORE:
                        self._spectral_recent.append({k: record[k] for k in (
                            "segment_id", "processing_started_unix_s", "text_ready_unix_s",
                            "audio_duration_s", "status") + _SPECTRAL_FIELDS if k in record})
                groups = [("all_segments", _ASR_ALL_METRICS)]
                if span["status"] in ("ready", "ready_fallback"):
                    groups.append(("ready", _ASR_READY_METRICS))
                for group, keys in groups:
                    for key in keys:
                        value = span.get(key)
                        if not _finite(value) or value < 0:
                            continue
                        stats = self._asr_stats[group].setdefault(key, {
                            "count": 0, "sum": 0.0, "max": 0.0, "recent": deque(maxlen=128),
                        })
                        stats["count"] += 1
                        stats["sum"] += value
                        stats["max"] = max(stats["max"], value)
                        stats["recent"].append(value)

    def count(self, key, amount=1):
        with self.lock:
            self.counters[key] += amount

    def gauge(self, key, value):
        if type(value) not in (int, float, bool, str):
            return
        with self.lock:
            self.gauges[key] = value

    def observe(self, key, seconds):
        with self.lock:
            stats = self.timings.setdefault(key, {"count": 0, "sum_s": 0.0, "max_s": 0.0, "recent_s": deque(maxlen=128)})
            stats["count"] += 1
            stats["sum_s"] += seconds
            stats["max_s"] = max(stats["max_s"], seconds)
            stats["recent_s"].append(seconds)

    @contextmanager
    def timed(self, key):
        started = self._now()
        try:
            yield
        finally:
            elapsed = max(0.0, self._now() - started)
            self.observe(key, elapsed)
            span = self._current_asr()
            if span is not None and key in _ASR_STAGES:
                field = _ASR_STAGES[key]
                span[field] += elapsed
                if key in ("asr.whisper", "asr.qwen"):
                    span["whisper_calls" if key == "asr.whisper" else "qwen_calls"] += 1

    def snapshot(self):
        with self.lock:
            timings = {}
            for key, stats in self.timings.items():
                values = sorted(stats["recent_s"])
                timings[key] = {"count": stats["count"], "mean_s": stats["sum_s"] / stats["count"],
                                "max_s": stats["max_s"], "recent_p95_s": values[max(0, (len(values) * 95 + 99) // 100 - 1)]}
            asr = {"recent_window": 128, "recent_segments": [dict(row) for row in self._asr_recent]}
            for group, fields in self._asr_stats.items():
                asr[group] = {}
                for name, stats in fields.items():
                    values = sorted(stats["recent"])
                    asr[group][name] = {"count": stats["count"], "mean": stats["sum"] / stats["count"],
                                        "max": stats["max"], "recent_p95": values[(len(values) * 95 + 99) // 100 - 1],
                                        "unit": "ratio" if name in ("rtf", "worker_rtf") else "s"}
            spectral = {"buckets": list(self._spectral_buckets),
                        "recent": [dict(row) for row in self._spectral_recent]}
            lifecycle = {"session_status": "running"}
            if self._ended_at is not None:
                lifecycle = {"session_status": "finished", "ended_at": self._ended_at}
            return {"schema": 1, "session": self.session, "started_at": self.started_at, **lifecycle,
                    "updated_at": time.time(), "counters": dict(self.counters),
                    "gauges": dict(self.gauges), "timings": timings, "asr_latency": asr,
                    "notification_spectral": spectral}

    def configure(self, path, *, history_dir=None, retention_days=DEFAULT_RETENTION_DAYS,
                  max_sessions=DEFAULT_MAX_SESSIONS, max_history_bytes=DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self.history = DiagnosticsHistory(history_dir or history_directory(self.path),
                                          retention_days=retention_days, max_sessions=max_sessions,
                                          max_bytes=max_history_bytes)
        self._previous_pending = True
        self.flush(force=True)

    def finish(self):
        """Фінальний знімок після завершення приймача й черги Telegram."""
        with self.lock:
            if self._ended_at is None:
                self._ended_at = time.time()
        # Не губимо завершення, якщо heartbeat саме записує попередній знімок.
        self.flush(force=True, wait=True)

    def flush(self, force=False, *, wait=False):
        if self.path is None or not self.flush_lock.acquire(blocking=wait):
            return
        log = logging.getLogger("radio.diagnostics")
        try:
            if not force and time.monotonic() - self.last_flush < 30:
                return
            previous_saved = True
            if self._previous_pending:
                try:
                    self.history.preserve_latest(self.path, self.session)
                    self._previous_pending = False
                except (OSError, ValueError):
                    previous_saved = False
                    log.warning("Не вдалося зберегти попередню сесію; її останній знімок не замінено")
            data = self.snapshot()
            try:
                self.history.save(data)
            except (OSError, ValueError):
                log.warning("Не вдалося оновити історію діагностики")
            if previous_saved:
                try:
                    atomic_write(self.path, data)
                except (OSError, ValueError):
                    log.warning("Не вдалося записати знімок діагностики")
            self.last_flush = time.monotonic()
        finally:
            self.flush_lock.release()


runtime = RuntimeDiagnostics()
