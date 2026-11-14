"""Bounded, content-free runtime measurements. Never changes retry decisions."""

import errno
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import contextmanager


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
    def __init__(self):
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.session = uuid.uuid4().hex
        self.started_at = time.time()
        self.counters = Counter()
        self.timings = {}
        self.gauges = {}
        self.path = None
        self.last_flush = float("-inf")

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
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(key, time.monotonic() - started)

    def snapshot(self):
        with self.lock:
            timings = {}
            for key, stats in self.timings.items():
                values = sorted(stats["recent_s"])
                timings[key] = {"count": stats["count"], "mean_s": stats["sum_s"] / stats["count"],
                                "max_s": stats["max_s"], "recent_p95_s": values[max(0, (len(values) * 95 + 99) // 100 - 1)]}
            return {"schema": 1, "session": self.session, "started_at": self.started_at,
                    "updated_at": time.time(), "counters": dict(self.counters),
                    "gauges": dict(self.gauges), "timings": timings}

    def configure(self, path):
        self.path = Path(path)
        self.flush(force=True)

    def flush(self, force=False):
        if self.path is None or not self.flush_lock.acquire(blocking=False):
            return
        temporary = None
        try:
            if not force and time.monotonic() - self.last_flush < 30:
                return
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix=".radio-diagnostics-", delete=False) as file:
                temporary = file.name
                json.dump(self.snapshot(), file, ensure_ascii=False)
            os.replace(temporary, self.path)
            self.last_flush = time.monotonic()
        except OSError:
            # Telemetry must never stop audio or cause a duplicate Telegram send.
            logging.getLogger("radio.diagnostics").warning("Не вдалося записати знімок діагностики")
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            self.flush_lock.release()


runtime = RuntimeDiagnostics()
