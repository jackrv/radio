"""Відкладені фрази: архів без Whisper, потім доробка без подій і Telegram."""
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from server.transcriber import Transcriber
from shared.recordings import RecordingStore


def pcm(seconds=1.0):
    return np.full(int(16000 * seconds), 1500, dtype=np.int16).tobytes()


class Recognizer:
    def __init__(self):
        self.unloads = 0
        self.warmups = 0

    def unload(self):
        self.unloads += 1

    def warmup(self):
        self.warmups += 1


def worker(**extra):
    w = Transcriber(segments=None, recognizer=Recognizer(), process_text=lambda t: t, event_processor=None,
                    sample_rate=16000, min_rms=0, normalize_audio=False, debug_rms=False, blacklist=(),
                    log=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None))
    for key, value in extra.items():
        setattr(w, key, value)
    return w


def test_whisper_unload_releases_model_and_mlx_cache(monkeypatch):
    import importlib
    import mlx.core as mx
    from shared.recognition import WhisperRecognizer

    class ModelHolder:
        model = object()
        model_path = "mlx-community/whisper-large-v3"

    original_import_module = importlib.import_module
    monkeypatch.setattr(
        importlib, "import_module",
        lambda name: SimpleNamespace(ModelHolder=ModelHolder)
        if name == "mlx_whisper.transcribe" else original_import_module(name),
    )
    cleared = []
    monkeypatch.setattr(mx, "clear_cache", lambda: cleared.append(True))
    recognizer = WhisperRecognizer("model", "uk", 16000, "", SimpleNamespace(info=lambda *a: None))
    recognizer.unload()
    assert ModelHolder.model is None and ModelHolder.model_path is None
    assert cleared == [True]


def test_deferred_phrase_is_recognised_without_events_into_the_same_record(monkeypatch):
    finished, seen = [], {}
    store = SimpleNamespace(next_deferred=lambda: ("a" * 32, pcm()),
                             finish_deferred=lambda rid, trace: finished.append((rid, trace)))
    w = worker(deferred=store)

    def process(pcm_, deferred_id=None):
        seen.update(dry=w._dry, deferred_id=deferred_id)
    monkeypatch.setattr(w, "process_segment", process)
    assert w._process_one_deferred() is True
    assert seen == {"dry": True, "deferred_id": "a" * 32} and w._dry is False

    w.deferred = SimpleNamespace(next_deferred=lambda: None, finish_deferred=None)
    assert w._process_one_deferred() is False
    calls = []
    w.deferred = SimpleNamespace(next_deferred=lambda: calls.append(1), finish_deferred=None)
    assert w._process_one_deferred() is False and calls == [], "без накопиченого архів не питаємо щосекунди"


def test_deferred_processing_updates_the_existing_record_without_events():
    finished, annotated = [], []

    class ReadyRecognizer(Recognizer):
        def transcribe(self, audio):
            return {"segments": [{"text": "Клен 48", "no_speech_prob": 0.0,
                                  "avg_logprob": -0.1, "compression_ratio": 1.0}]}

        @staticmethod
        def extract_text(result):
            return " ".join(segment["text"] for segment in result["segments"])

    w = worker(
        recorder=SimpleNamespace(submit=lambda *args: pytest.fail("не створюємо другий запис")),
        deferred=SimpleNamespace(finish_deferred=lambda rid, trace: finished.append((rid, trace))),
        event_processor=SimpleNamespace(process=lambda *args, **kwargs: pytest.fail("не створюємо подію"),
                                        annotate=annotated.append),
    )
    w.recognizer = ReadyRecognizer()
    w._dry = True
    w.process_segment(pcm(), deferred_id="b" * 32)
    assert finished[0][0] == "b" * 32
    assert annotated == ["Клен 48"], "аналіз і ролі — як у живому ефірі"
    assert finished[0][1]["raw_text"] == "Клен 48"
    assert finished[0][1]["final_text"] == "Клен 48"
    assert finished[0][1]["status"] == "ready"


def test_archive_keeps_deferred_records_and_finishes_them_in_place(tmp_path):
    store = RecordingStore(tmp_path / "archive", retention_days=7, max_bytes=50_000)
    deferred = store.save(pcm(), 16000, {"status": "deferred"})
    noise = store.save(pcm(), 16000, {"status": "low_rms"})
    store.prune()
    assert store.listing(view="deferred")["total"] == 1
    assert store.listing(view="filtered")["total"] == 0, "відкладений запис не дублюється у відфільтрованих"
    with store.connect() as db:
        ids = {r[0] for r in db.execute("SELECT id FROM recordings")}
    assert deferred in ids and noise not in ids, "накопичене не підрізається, доки не розпізнано"
    record_id, audio = store.next_deferred()
    assert record_id == deferred and audio == pcm()
    created = store.get(deferred)["created"]
    assert store.created_at(deferred) == created
    store.finish_deferred(deferred, {"status": "ready", "raw_text": "клен 48", "final_text": "Клен 48"})
    item = store.get(deferred)
    assert item["status"] == "ready" and item["data"]["final_text"] == "Клен 48" and item["data"]["deferred"] is True
    assert item["created"] == created and store.next_deferred() is None
    assert store.listing(view="deferred")["total"] == 0
