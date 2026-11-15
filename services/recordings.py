"""Private, bounded audio review corpus. Operator references never train rules."""
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid
import wave
from contextvars import ContextVar
from contextlib import contextmanager

import numpy as np

_trace = ContextVar("recording_trace", default=None)
UNSET = object()
# Звук із кімнати (клавіатура, кроки) — не радіоефір: окремо від шуму ефіру в метриках.
NON_SPEECH = ("noise", "notification", "room")
# Live annotation queues; frozen experimental samples stay in tools/asr_lab.py.
REVIEW_QUEUES = {
    "A": ("Усе, що бачив Whisper", "status IN ('ready','empty_text','blacklist')"),
    "B": ("Тихі поруч із порогом (−36…−34 dBFS)",
          "status='low_rms' AND json_extract(data,'$.signal.rms_dbfs')>=-36 "
          "AND json_extract(data,'$.signal.rms_dbfs')<-34"),
    "C": ("Тихі нижче −36 dBFS", "status='low_rms' AND json_extract(data,'$.signal.rms_dbfs')<-36"),
    "D": ("Відкинуті VAD, мовлення від 300 мс", "status='vad_short' AND json_extract(data,'$.speech_ms')>=300"),
}
ID = re.compile(r"[0-9a-f]{32}\Z")


def note(**fields):
    trace = _trace.get()
    if trace is not None:
        trace.update(fields)



def json_values(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_values(item) for item in value]
    return value

def signal_metrics(pcm, sr):
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    if not x.size:
        return {"duration_s": 0}
    window = max(1, sr * 30 // 1000)
    frames = [float(np.sqrt(np.mean(x[i:i + window] ** 2))) for i in range(0, len(x), window)]
    rms = float(np.sqrt(np.mean(x ** 2)))
    return {"duration_s": len(x) / sr, "rms": rms, "rms_dbfs": 20 * math.log10(max(rms, 1e-10)),
            "peak": float(np.max(np.abs(x))), "clipping_ratio": float(np.mean(np.abs(x) >= 0.999)),
            "dc_offset": float(np.mean(x)), "zero_ratio": float(np.mean(x == 0)),
            "frame_rms_p10": float(np.percentile(frames, 10)), "frame_rms_p90": float(np.percentile(frames, 90))}


def words(text):
    return re.findall(r"[^\W_]+", text.casefold())


def distance(a, b):
    from rapidfuzz.distance import Levenshtein
    return Levenshtein.distance(a, b)


def scores(reference, hypothesis):
    a, b = words(reference), words(hypothesis)
    # Non-speech references have no WER denominator; report false text separately.
    chars_a, chars_b = " ".join(a), " ".join(b)
    from core.word_learning import number_groups
    return {"char_errors": distance(chars_a, chars_b), "reference_chars": len(chars_a),
            "cer": distance(chars_a, chars_b) / len(chars_a) if chars_a else None,
            "word_errors": distance(a, b), "reference_words": len(a),
            "wer": distance(a, b) / len(a) if a else None,
            "number_match": number_groups(reference) == number_groups(hypothesis)}


class RecordingStore:
    def __init__(self, directory, *, retention_days=7, max_bytes=1024 ** 3):
        self.root = Path(directory)
        self.retention_days = retention_days
        self.max_bytes = max_bytes

    @contextmanager
    def connect(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("Recording directory must not be a symlink")
        self.root.chmod(0o700)
        path = self.root / "index.sqlite3"
        if path.is_symlink():
            raise ValueError("Recording database must not be a symlink")
        db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
            bytes INTEGER NOT NULL, data TEXT NOT NULL, reference TEXT,
            label TEXT, revision INTEGER NOT NULL DEFAULT 0, reviewed_at REAL,
            reference_recipient TEXT, reference_sender TEXT, multiple_messages INTEGER)""")
        try:
            # Адитивні міграції: старі записи й розмітка не змінюються.
            added = {"reference_recipient": "TEXT", "reference_sender": "TEXT", "multiple_messages": "INTEGER"}
            columns = {row[1] for row in db.execute("PRAGMA table_info(recordings)")}
            if not set(added) <= columns:
                db.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in db.execute("PRAGMA table_info(recordings)")}
                for name, kind in added.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE recordings ADD COLUMN {name} {kind}")
                db.commit()
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, pcm, sr, data):
        record_id = uuid.uuid4().hex
        target = self.root / (record_id + ".wav")
        data = json_values(dict(data, signal=signal_metrics(pcm, sr), sample_rate=sr))
        with self.connect() as db:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    with wave.open(output, "wb") as wav:
                        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sr)
                        wav.writeframes(pcm)
                db.execute("INSERT INTO recordings(id,created,status,bytes,data) VALUES(?,?,?,?,?)",
                           (record_id, time.time(), data.get("status", "error"), target.stat().st_size,
                            json.dumps(data, ensure_ascii=False, allow_nan=False)))
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        self.prune()
        return record_id

    def prune(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,created,bytes,reference FROM recordings ORDER BY created DESC").fetchall()
            used, cutoff = 0, time.time() - self.retention_days * 86400
            for row in rows:
                # Reviewed material is the evaluation corpus and must survive retention.
                if row["reference"] is not None:
                    used += row["bytes"]
            for row in rows:
                if row["reference"] is not None:
                    continue
                if row["created"] < cutoff or used + row["bytes"] > self.max_bytes:
                    (self.root / (row["id"] + ".wav")).unlink(missing_ok=True)
                    db.execute("DELETE FROM recordings WHERE id=? AND reference IS NULL", (row["id"],))
                else:
                    used += row["bytes"]

    @staticmethod
    def decode(row):
        result = dict(row)
        result["data"] = json.loads(result["data"])
        if result["reference"] is not None:
            result["quality"] = {stage: scores(result["reference"], result["data"].get(stage, ""))
                                 for stage in ("raw_text", "final_text")}
        result["role_quality"] = {}
        for role in ("recipient", "sender"):
            reference = result[f"reference_{role}"]
            predicted = result["data"].get(f"predicted_{role}")
            result["role_quality"][role] = {
                "reference": reference, "predicted": predicted,
                "match": (words(reference) == words(predicted)) if reference is not None and predicted is not None else None,
            }
        return result

    def listing(self, offset=0, limit=25, review="all", group="all", ids=None, queue=""):
        reviewed = {"all": "1", "pending": "reference IS NULL", "reviewed": "reference IS NOT NULL"}[review]
        # Rejected short/quiet/blacklisted material belongs outside the main
        # review list regardless of its audio duration.
        short = "(status IN ('vad_short','low_rms','blacklist') OR COALESCE(json_extract(data,'$.signal.duration_s'),999999)<=2.0)"
        shadow = "COALESCE(json_extract(data,'$.text_filter_shadow.additional'),0)=1"
        grouped = {"all": "1", "normal": f"NOT {short}", "short": short, "shadow": shadow}[group]
        params = ()
        if queue:
            grouped = " OR ".join(f"({condition})" for _, condition in REVIEW_QUEUES.values()) \
                if queue == "all" else REVIEW_QUEUES[queue][1]
        elif ids is not None:
            # Optional frozen ID list used by offline callers.
            grouped, params = "id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(ids)),)
        where = f"({reviewed}) AND ({grouped})"
        with self.connect() as db:
            db.execute("BEGIN")
            counts = db.execute(f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {short} THEN 1 ELSE 0 END),0), "
                                f"COALESCE(SUM({shadow}),0) FROM recordings WHERE {reviewed}").fetchone()
            groups = {"all": counts[0], "short": counts[1], "normal": counts[0] - counts[1], "shadow": counts[2]}
            total = db.execute(f"SELECT COUNT(*) FROM recordings WHERE {where}", params).fetchone()[0]
            # New recordings append to a live queue, keeping loaded pages stable.
            order = "created ASC,id ASC" if queue else "created DESC,id DESC"
            rows = db.execute(f"SELECT * FROM recordings WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
        return {"items": [self.decode(row) for row in rows], "total": total, "groups": groups, "short_max_seconds": 2}

    def queue_progress(self):
        """Current archive membership: new arrivals enter without rewriting an ID file."""
        result = []
        with self.connect() as db:
            db.execute("BEGIN")
            for key, (title, condition) in REVIEW_QUEUES.items():
                present, reviewed = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(reference IS NOT NULL),0) FROM recordings "
                    f"WHERE {condition}").fetchone()
                result.append({"key": key, "title": title, "total": present,
                               "present": present, "reviewed": reviewed, "pending": present - reviewed})
        return result

    def get(self, record_id):
        if not ID.fullmatch(record_id):
            raise LookupError("Запис не знайдено")
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise LookupError("Запис не знайдено")
        return self.decode(row)

    def audio(self, record_id):
        self.get(record_id)
        path = self.root / (record_id + ".wav")
        if path.is_symlink():
            raise LookupError("Аудіо недоступне")
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise LookupError("Аудіо недоступне") from None

    def review(self, record_id, reference, label, revision, *,
               reference_recipient=UNSET, reference_sender=UNSET, multiple_messages=UNSET):
        self.get(record_id)
        with self.connect() as db:
            fields, values = ["reference=?", "label=?", "revision=revision+1", "reviewed_at=?"], [reference, label, time.time()]
            if label in NON_SPEECH:
                multiple_messages = None
            if multiple_messages is not UNSET:
                # Кілька передач в одному записі (злиття сегментів VAD): None — не відмічено.
                fields.append("multiple_messages=?")
                values.append(None if multiple_messages is None else int(bool(multiple_messages)))
            for name, value in (("reference_recipient", reference_recipient), ("reference_sender", reference_sender)):
                if label in NON_SPEECH:
                    value = ""
                if value is not UNSET:
                    fields.append(f"{name}=?")
                    values.append(value.strip() if isinstance(value, str) else None)
            changed = db.execute(f"UPDATE recordings SET {','.join(fields)} WHERE id=? AND revision=?",
                                 (*values, record_id, revision)).rowcount
            if not changed:
                raise ValueError("Запис змінено в іншому вікні. Оновіть сторінку.")
        return self.get(record_id)

    def summary(self):
        with self.connect() as db:
            statuses = dict(db.execute("SELECT status,COUNT(*) FROM recordings GROUP BY status").fetchall())
            rows = db.execute("SELECT * FROM recordings WHERE reference IS NOT NULL").fetchall()
            size = db.execute("SELECT COALESCE(SUM(bytes),0) FROM recordings").fetchone()[0]
            shadow_counts = db.execute("SELECT "
                "COALESCE(SUM(json_extract(data,'$.text_filter_shadow.evaluated')=1),0), "
                "COALESCE(SUM(json_extract(data,'$.text_filter_shadow.additional')=1),0) FROM recordings").fetchone()
        quality = {}
        decoded = [self.decode(row) for row in rows]
        shadow_reviewed = [r for r in decoded if r['data'].get('text_filter_shadow', {}).get('additional')]
        shadow = {"evaluated": shadow_counts[0], "candidates": shadow_counts[1], "reviewed": len(shadow_reviewed),
                  **{label: sum(r['label'] == label for r in shadow_reviewed)
                     for label in ("speech", "noise", "notification", "room", "unclear")}}
        for stage in ("raw_text", "final_text"):
            speech = [r for r in decoded if r["label"] == "speech"]
            errors = sum(r["quality"][stage]["word_errors"] for r in speech)
            count = sum(r["quality"][stage]["reference_words"] for r in speech)
            chars = sum(r["quality"][stage]["reference_chars"] for r in speech)
            char_errors = sum(r["quality"][stage]["char_errors"] for r in speech)
            quality[stage] = {"cer": char_errors / chars if chars else None, "wer": errors / count if count else None, "reference_words": count,
                              "word_errors": errors, "reviewed_speech": len(speech)}
        roles = {}
        for role in ("recipient", "sender"):
            matches = [row["role_quality"][role]["match"] for row in decoded
                       if row["label"] in ("speech", "unclear") and row["role_quality"][role]["match"] is not None]
            roles[role] = {"evaluated": len(matches), "correct": sum(matches),
                           "accuracy": sum(matches) / len(matches) if matches else None}
        return {"roles": roles, "statuses": statuses, "reviewed": len(rows), "bytes": size, "quality": quality,
                "text_filter_shadow": shadow,
                "retention_days": self.retention_days, "max_bytes": self.max_bytes,
                "reviewed_protected": True,
                "missed_speech": sum(r["label"] == "speech" and not r["data"].get("raw_text") for r in decoded),
                "raw_false_text": sum(r["label"] in ("noise", "notification") and bool(r["data"].get("raw_text")) for r in decoded),
                "false_text": sum(r["label"] in ("noise", "notification") and bool(r["data"].get("final_text")) for r in decoded),
                # Кімнатний шум рахується окремо: на робочому прототипі його не буде.
                "room": sum(r["label"] == "room" for r in decoded),
                "room_false_text": sum(r["label"] == "room" and bool(r["data"].get("final_text")) for r in decoded),
                "multiple_messages": sum(r.get("multiple_messages") == 1 for r in decoded),
                "speech_reviewed": sum(r["label"] in ("speech", "unclear") for r in decoded)}


class RecordingArchive:
    """Bounded asynchronous writer: disk failures never stop recognition."""
    def __init__(self, store, settings, log):
        self.store, self.settings, self.log = store, settings, log
        self.pending = queue.Queue(maxsize=64)
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="recording-writer", daemon=True)
        self.thread.start()

    def submit(self, pcm, sr, data):
        from core.diagnostics import runtime
        try:
            if self.closed:
                raise queue.Full
            self.pending.put_nowait((bytes(pcm), sr, dict(data, settings=self.settings)))
        except queue.Full:
            runtime.count("recordings.dropped")

    def vad_short(self, pcm, sr, speech_ms):
        self.submit(pcm, sr, {"status": "vad_short", "speech_ms": speech_ms})

    def _run(self):
        from core.diagnostics import runtime
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                self.store.save(*item)
                runtime.count("recordings.saved")
            except Exception:
                runtime.count("recordings.write_error")
                self.log.exception("Не вдалося зберегти аудіозапис для перевірки")
            finally:
                self.pending.task_done()

    def close(self):
        self.closed = True
        try:
            self.pending.put(None, timeout=5)
        except queue.Full:
            return
        self.thread.join(timeout=10)
