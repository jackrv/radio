"""Мовчазний відсів шматків Whisper, перелік назв і одиночний позивний — пороги з панелі (docs/filters.md)."""
import json
from unittest.mock import Mock

import numpy as np

from shared.callsigns import Callsigns, command_dictionary
from shared.error_analysis import filter_stats
from shared.recordings import RecordingStore
from shared.text_analysis import analyze_text
from shared.whisper_result import dropped_reason, extract_text, is_looped

LOOP = {"text": "дуб, норок, норок, норок", "compression_ratio": 18.5, "avg_logprob": -0.1, "no_speech_prob": 0.0}
QUIET = {"text": "дякую", "compression_ratio": 1.0, "avg_logprob": -1.2, "no_speech_prob": 0.9}
GOOD = {"text": "Клен 48, прийом", "compression_ratio": 1.1, "avg_logprob": -0.2, "no_speech_prob": 0.01}


def test_defaults_are_unchanged_and_zero_switches_off():
    result = {"segments": [GOOD, LOOP, QUIET]}
    assert extract_text(result) == "Клен 48, прийом"
    assert [dropped_reason(s) for s in result["segments"]] == [None, "loop", "silence"]
    assert extract_text(result, compression_limit=0) == "Клен 48, прийом дуб, норок, норок, норок"
    assert extract_text(result, no_speech_prob=0) == "Клен 48, прийом дякую"
    assert extract_text(result, compression_limit=20) == "Клен 48, прийом дуб, норок, норок, норок"
    assert is_looped(result) and not is_looped(result, compression_limit=0)


def test_bare_callsign_rule_can_be_switched_off():
    callsigns, commands, watch = Callsigns.from_lines(["клен"]), command_dictionary([]), Callsigns.from_lines([])
    assert analyze_text("Клен", callsigns, commands, watch, set(), Mock()).rejected == "bare_callsign"
    assert analyze_text("Клен", callsigns, commands, watch, set(), Mock(), reject_bare_callsign=False).rejected is None


def test_segment_filters_are_counted_from_stored_segments(tmp_path):
    store = RecordingStore(tmp_path / "rec")
    pcm = np.zeros(1600, "<i2").tobytes()
    lost = store.save(pcm, 16000, {"status": "ready", "capture_source": "legacy", "whisper_segments": [GOOD, QUIET]})
    noise = store.save(pcm, 16000, {"status": "empty_text", "capture_source": "legacy", "whisper_segments": [LOOP]})
    store.save(pcm, 16000, {"status": "empty_text", "capture_source": "legacy", "whisper_segments": [LOOP]})
    # Запис із власними порогами: шматок-тиша тоді не відкидався — не рахується.
    store.save(pcm, 16000, {"status": "ready", "capture_source": "legacy", "whisper_segments": [QUIET],
                            "segment_limits": {"no_speech_prob": 0}})
    store.review(lost, "Клен 48, прийом, дякую", "speech", 0)
    store.review(noise, "", "noise", 0)
    rows = {r["status"]: r for r in filter_stats(store, profile="pi-webcam")}
    assert (rows["segment_silence"]["dropped"], rows["segment_silence"]["lost"]) == (1, 1), "без «дякую» далі від еталона"
    loop = rows["segment_loop"]
    assert (loop["dropped"], loop["caught"], loop["unlabelled"]) == (2, 1, 1)


def test_receiver_applies_panel_limits_and_records_them(tmp_path):
    from tests.test_asr_service import LOG, Events, phrase
    from server.asr_service import SpoolRecorder
    from server.transcriber import Transcriber
    from shared.recognition import WhisperRecognizer

    class Loop:
        prompt = ""
        def transcribe(self, audio, prompt=None):
            return {"segments": [dict(LOOP)]}
        extract_text = staticmethod(WhisperRecognizer.extract_text)

    store = RecordingStore(tmp_path / "rec")
    worker = Transcriber(segments=None, recognizer=Loop(), process_text=lambda t: t, event_processor=Events(),
                         sample_rate=16000, min_rms=0, normalize_audio=False, debug_rms=False, blacklist=(),
                         log=LOG, recorder=SpoolRecorder(store, {}))
    worker.process_segment(phrase())
    worker.segment_limits = {"compression_limit": 0, "no_speech_prob": 0.6, "no_speech_logprob": -0.8}
    worker.process_segment(phrase())
    with store.connect() as db:
        rows = [json.loads(d) for (d,) in db.execute("SELECT data FROM recordings ORDER BY created")]
    assert rows[0]["status"] == "empty_text" and "segment_limits" not in rows[0]
    assert rows[1]["raw_text"].startswith("дуб") and rows[1]["segment_limits"]["compression_limit"] == 0
