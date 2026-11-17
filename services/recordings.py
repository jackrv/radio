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
# Групи для розмітки тексту: Whisper щось розпізнав (або зациклився на довгому
# мовленні) і запис довший за 2 с; «Ефір» — серед них ті, де є позивний чи подія.
SPEECH_GROUP = ("(status IN ('ready','empty_text') "
                "AND COALESCE(json_extract(data,'$.signal.duration_s'),0) > 2.0)")
RADIO_GROUP = ("(json_extract(data,'$.fix_id') IS NOT NULL "
               "OR COALESCE(json_extract(data,'$.predicted_recipient'),'') <> '' "
               "OR COALESCE(json_extract(data,'$.predicted_sender'),'') <> '')")
# Види для панелі: архів ділиться на три частини без перетину — ефір (позивний чи
# подія), інше мовлення, усе відсіяне. Ключ -> (стан перевірки, умова).
VIEWS = {
    "radio": ("pending", RADIO_GROUP),
    "speech": ("pending", f"({SPEECH_GROUP} AND NOT {RADIO_GROUP})"),
    "filtered": ("pending", f"(NOT {SPEECH_GROUP} AND NOT {RADIO_GROUP})"),
    "shadow": ("pending", "COALESCE(json_extract(data,'$.text_filter_shadow.additional'),0)=1"),
    "reviewed": ("reviewed", "1"),
    "telegram": ("all", "json_extract(data,'$.fix_id') IS NOT NULL"),   # усе, що надіслано в Telegram
    "all": ("all", "1"),
}
_REVIEW_SQL = {"all": "1", "pending": "reference IS NULL", "reviewed": "reference IS NOT NULL"}
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


_SEARCH_ID = re.compile(r"[0-9a-f]{4,32}")
_SEARCH_EVENT = re.compile(r"(?:№|#|db:)\s*([1-9][0-9]{0,17})")


def search_condition(query):
    """Умова SQL для пошуку: «1a2b3c4d, 5e6f7a8b», «№82» або шматок тексту.

    Якщо всі слова запиту — ID записів (від 4 шістнадцяткових знаків, з початку)
    чи номери подій, шукаються будь-які з них. Весь запит завжди шукається й як
    шматок еталона або тексту Whisper без урахування регістру: «2684» — і число.
    """
    text = query.strip().casefold()
    text_condition = ("instr(casefold(COALESCE(reference,'') || ' ' || "
                      "COALESCE(json_extract(data,'$.raw_text'),'')), ?) > 0")
    terms = [t for t in re.split(r"[\s,;]+", text) if t]
    ids, events = [], []
    for term in terms:
        event = _SEARCH_EVENT.fullmatch(term)
        if event:
            events.append(f"db:{event[1]}")
        elif _SEARCH_ID.fullmatch(term):
            ids.append(term)
        else:
            break
    else:
        conditions = ["id LIKE ?"] * len(ids) + ["json_extract(data,'$.fix_id')=?"] * len(events)
        return " OR ".join([*conditions, text_condition]), (*(f"{i}%" for i in ids), *events, text)
    return text_condition, (text,)


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
        # lower()/LIKE у SQLite не знають кирилиці; пошук за текстом — через casefold.
        db.create_function("casefold", 1, lambda value: (value or "").casefold(), deterministic=True)
        db.execute("""CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
            bytes INTEGER NOT NULL, data TEXT NOT NULL, reference TEXT,
            label TEXT, revision INTEGER NOT NULL DEFAULT 0, reviewed_at REAL,
            reference_recipient TEXT, reference_sender TEXT, multiple_messages INTEGER,
            reference_source TEXT)""")
        try:
            # Адитивні міграції: старі записи й розмітка не змінюються.
            # reference_source: NULL — розмітка оператора в панелі, 'miniapp' — правка з Mini App.
            added = {"reference_recipient": "TEXT", "reference_sender": "TEXT", "multiple_messages": "INTEGER",
                     "reference_source": "TEXT"}
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

    def view_counts(self, since=None):
        """Скільки записів у кожному виді (за період ``since`` — unix-час, або весь архів)."""
        period = "created >= ?" if since else "1"
        sums = ", ".join(f"COALESCE(SUM(CASE WHEN ({_REVIEW_SQL[review]}) AND ({condition}) THEN 1 ELSE 0 END),0)"
                         for review, condition in VIEWS.values())
        with self.connect() as db:
            row = db.execute(f"SELECT {sums} FROM recordings WHERE {period}", (since,) if since else ()).fetchone()
        return dict(zip(VIEWS, row))

    def listing(self, offset=0, limit=25, *, view="all", search="", edits=None, since=None):
        review, condition = VIEWS[view]
        reviewed = _REVIEW_SQL[review]
        params = ()
        if search.strip():
            reviewed, (condition, params) = "1", search_condition(search)
        where = f"({reviewed}) AND ({condition})"
        if edits == "miniapp":
            where += " AND reference_source='miniapp'"
        elif edits is not None:
            if not search.strip():
                where, params = f"({reviewed})", ()
            where += " AND json_extract(data,'$.fix_id') IN (SELECT value FROM json_each(?))"
            params = (*params, json.dumps(sorted(set(edits))))
        if since is not None:
            where += " AND created >= ?"
            params = (*params, float(since))
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT COUNT(*) FROM recordings WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM recordings WHERE {where} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
        return {"items": [self.decode(row) for row in rows], "total": total}

    def get(self, record_id):
        if not ID.fullmatch(record_id):
            raise LookupError("Запис не знайдено")
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise LookupError("Запис не знайдено")
        return self.decode(row)

    def labelled_speech(self):
        """Розмічене мовлення: правильний текст оператора й сирий текст Whisper."""
        with self.connect() as db:
            rows = db.execute("SELECT id, reference, data FROM recordings "
                              "WHERE reference IS NOT NULL AND label IN ('speech','unclear')").fetchall()
        return [{"id": row["id"], "reference": row["reference"],
                 "raw_text": json.loads(row["data"]).get("raw_text", "")} for row in rows]

    def labelled_texts(self):
        """Усі розмічені записи: мітка, статус, текст Whisper і правильний текст."""
        with self.connect() as db:
            rows = db.execute("SELECT label, status, reference, data FROM recordings "
                              "WHERE reference IS NOT NULL").fetchall()
        return [{"label": row["label"], "status": row["status"], "reference": row["reference"],
                 "raw_text": json.loads(row["data"]).get("raw_text", "")} for row in rows]

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
            fields = ["reference=?", "label=?", "revision=revision+1", "reviewed_at=?", "reference_source=NULL"]
            values = [reference, label, time.time()]
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

    def label_from_correction(self, fix_id, reference):
        """Правка з Mini App стає еталоном запису, з якого вийшло це повідомлення.

        Розмітку оператора в панелі не змінює; попередню правку з Mini App
        замінює новішою. Повертає ID змінених записів.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [row[0] for row in db.execute(
                "SELECT id FROM recordings WHERE json_extract(data,'$.fix_id')=? "
                "AND (label IS NULL OR reference_source='miniapp')", (fix_id,))]
            for record_id in ids:
                db.execute("UPDATE recordings SET reference=?, label='speech', reference_source='miniapp', "
                           "revision=revision+1, reviewed_at=? WHERE id=?", (reference, time.time(), record_id))
        return ids

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


def label_archive_from_correction(db_path, fix_id, original, corrected):
    """Переносить правку з Mini App в архів поруч із базою подій, якщо архів є.

    Правка лише регістру чи пунктуації (у т. ч. збереження без змін) не є
    перевіреним еталоном і не переноситься.
    """
    from callsigns import basic_normalize
    if basic_normalize(original or "") == basic_normalize(corrected):
        return []
    root = Path(db_path).resolve().parent / ".local-recordings"
    if not (root / "index.sqlite3").is_file():
        return []
    return RecordingStore(root).label_from_correction(fix_id, corrected)


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
