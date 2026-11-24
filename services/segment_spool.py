"""Дискова черга фраз між ефіром і розпізнаванням на Pi: файл = одна фраза, без SQLite.

Запис атомарний (тимчасовий файл → fsync → rename): після вимкнення живлення лишаються
лише цілі фрази. Читач видаляє файл тільки після збереження результату.
"""
import json
import os
import struct
import time
import uuid
from pathlib import Path

from core.diagnostics import runtime
from core.segment_timing import SegmentTiming, TimedSegment

MAX_BYTES = 2 * 1024 ** 3   # ~17 год мовлення 16 кГц


class SegmentSpool:
    def __init__(self, directory, max_bytes=MAX_BYTES):
        self.root, self.max_bytes = Path(directory), max_bytes
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def files(self):
        return sorted(self.root.glob("*.seg"))

    def put(self, pcm, sample_rate):
        timing = getattr(pcm, "timing", None)
        ended = getattr(timing, "audio_end_unix_s", None) or time.time()
        head = json.dumps({"sample_rate": sample_rate, "audio_end_unix_s": ended,
                           "vad": getattr(pcm, "vad", None)}).encode()
        name = f"{int(ended * 1000):015d}-{uuid.uuid4().hex}"
        temp = self.root / (name + ".tmp")
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(struct.pack("<I", len(head)) + head + bytes(pcm))
            out.flush()
            os.fsync(out.fileno())
        os.rename(temp, self.root / (name + ".seg"))
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._limit()

    def _limit(self):
        files = [(f, f.stat().st_size) for f in self.files() if f.exists()]
        total = sum(size for _, size in files)
        for path, size in files[:-1]:   # найновішу фразу не чіпаємо
            if total <= self.max_bytes:
                break
            path.unlink(missing_ok=True)
            runtime.count("spool.dropped_oldest")
            total -= size

    @staticmethod
    def read(path):
        """(record_id, pcm з часом, sample_rate, час кінця); ValueError — файл пошкоджено."""
        raw = Path(path).read_bytes()
        try:
            size = struct.unpack_from("<I", raw)[0]
            head = json.loads(raw[4:4 + size])
            ended, rate = float(head["audio_end_unix_s"]), int(head["sample_rate"])
        except (struct.error, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Пошкоджений файл черги") from exc
        pcm = raw[4 + size:]
        if not pcm or len(pcm) % 2:
            raise ValueError("Пошкоджений звук у черзі")
        segment = TimedSegment(pcm, SegmentTiming(
            enqueued_at=time.perf_counter(), enqueued_unix_s=time.time(), audio_end_unix_s=ended))
        segment.vad = head.get("vad") if isinstance(head.get("vad"), dict) else None
        return Path(path).stem.split("-", 1)[1], segment, rate, ended

    def quarantine(self, path):
        os.rename(path, Path(path).with_suffix(".bad"))
