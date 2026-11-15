"""Офлайн-стенд ASR: метрики, поділ і незмінність архіву (тимчасові дані, без моделей)."""

import hashlib
import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest

from tools import asr_lab


def test_split_keeps_one_broadcast_block_together_and_is_deterministic():
    base = 1_800_000_000 - 1_800_000_000 % asr_lab.SPLIT_BLOCK_S
    assert len({asr_lab.split_of(base + offset) for offset in (0, 60, 1199)}) == 1
    splits = [asr_lab.split_of(base + block * asr_lab.SPLIT_BLOCK_S) for block in range(300)]
    assert splits == [asr_lab.split_of(base + block * asr_lab.SPLIT_BLOCK_S) for block in range(300)]
    assert 0.2 < splits.count("holdout") / len(splits) < 0.45


def test_number_check_does_not_rely_only_on_app_parser():
    score = asr_lab.score_text("Вишня 956, я Вишня 700", "Вишня дев'ятсот п'ятдесят шість, я Вишня 700")
    assert score["digits_match"] is False, "сирі цифри — незалежна перевірка"
    assert "app_numbers_match" in score


def result(label, reference, hypothesis, **roles):
    item = {"label": label, "reference": reference, "hypothesis": hypothesis, "status": "ready",
            "signal": {"duration_s": 3, "rms_dbfs": -30}, "timing": {"total_s": 1.0},
            "reference_recipient": None, "reference_sender": None, **roles}
    if reference is not None:
        item["score"] = asr_lab.score_text(reference, hypothesis)
    return item


def test_summary_counts_missed_speech_false_text_and_excludes_unknown_roles():
    results = [
        result("speech", "вишня 210 прийом", "вишня 210 прийом", reference_recipient="вишня 210",
               hypothesis_recipient="вишня 210", reference_recipient_parsed="вишня 210"),
        result("speech", "клен 300 як чути", "", reference_recipient="клен 300",
               hypothesis_recipient="", reference_recipient_parsed="клен 300"),
        result("noise", "", "дякую за перегляд"),
        result("unclear", "", "щось"),
        result("speech", "так", "так", reference_recipient=None, hypothesis_recipient="вишня 210"),
    ]
    summary = asr_lab.summarize(results)
    assert summary["speech_records"] == 3 and summary["missed_speech"] == 1
    assert summary["wer"] == pytest.approx(4 / 8)
    assert summary["false_text_on_non_speech"] == 1 and summary["non_speech_records"] == 1
    assert summary["recipient_evaluated"] == 2, "невідома роль не входить у знаменник"
    assert summary["recipient_accuracy_from_hypothesis"] == 0.5
    assert summary["recipient_accuracy_from_reference_text"] == 1.0


def make_archive(path):
    from services.recordings import RecordingStore
    store = RecordingStore(path)
    t = np.arange(16000 * 2) / 16000
    pcm = (0.2 * np.sin(2 * np.pi * 300 * t) * 32767).astype("<i2").tobytes()
    speech = store.save(pcm, 16000, {"status": "ready", "raw_text": "стара"})
    quiet = store.save((np.frombuffer(pcm, "<i2") // 50).astype("<i2").tobytes(), 16000, {"status": "low_rms"})
    store.review(speech, "Вишня 210, я Клен 300", "speech", 0, reference_recipient="Вишня 210", reference_sender="Клен 300")
    return store, speech, quiet


def test_run_reads_archive_only_and_saves_results_with_parameters(tmp_path, monkeypatch):
    archive = tmp_path / "recordings"
    store, speech, quiet = make_archive(archive)
    db = tmp_path / "events.db"
    from database import init_db
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',?,?)",
                         [(1, "вишня: 210"), (2, "клен: 300")])
    index = archive / "index.sqlite3"
    before = hashlib.sha256(index.read_bytes()).hexdigest()

    class FakeWhisper:
        def __init__(self, *args):
            self.prompt = args[3]

        def transcribe(self, audio):
            return {"segments": [{"text": "Вишня 210, я Клен 300", "avg_logprob": -0.2, "no_speech_prob": 0.0,
                                  "compression_ratio": 1.1, "temperature": 0.0, "start": 0, "end": 2}]}

        extract_text = staticmethod(__import__("core.recognition", fromlist=["x"]).WhisperRecognizer.extract_text)

    monkeypatch.setattr("config.DB_PATH", str(db))
    monkeypatch.setattr(asr_lab, "OUTPUT", tmp_path / "experiments")
    with patch("core.recognition.WhisperRecognizer", FakeWhisper):
        asr_lab.main(["--archive", str(archive), "run", "--name", "smoke", "--variant", "rules", "--min-rms", "0.02"])

    assert hashlib.sha256(index.read_bytes()).hexdigest() == before, "архів не змінюється"
    run_dir = next((tmp_path / "experiments").iterdir())
    params = json.loads((run_dir / "params.json").read_text())
    assert params["variant"] == "rules" and params["prompt_chars"] > 0 and params["min_rms_gate"] == 0.02
    rows = {r["id"]: r for r in map(json.loads, (run_dir / "results.jsonl").read_text().splitlines())}
    assert rows[speech].get("error") is None, rows[speech]
    assert rows[speech]["score"]["word_errors"] == 0
    assert (rows[speech]["hypothesis_recipient"], rows[speech]["hypothesis_sender"]) == ("вишня 210", "клен 300")
    assert rows[quiet]["stage"] == "rms_gate" and rows[quiet]["hypothesis"] == ""
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["summary"]["wer"] == 0 and metrics["summary"]["recipient_accuracy_from_hypothesis"] == 1.0
    assert oct(run_dir.parent.stat().st_mode & 0o777) == "0o700"


def test_queue_is_stratified_deterministic_and_skips_reviewed_records():
    def record(i, status, rms=-30, speech_ms=None, label=None):
        return {"id": f"{i:032x}", "status": status, "label": label,
                "data": {"signal": {"rms_dbfs": rms}, "speech_ms": speech_ms}}
    records = ([record(i, "ready") for i in range(5)] + [record(10, "ready", label="speech")]
               + [record(20 + i, "low_rms", rms=-35) for i in range(30)]
               + [record(60 + i, "low_rms", rms=-39) for i in range(30)]
               + [record(100 + i, "vad_short", speech_ms=350) for i in range(30)]
               + [record(200 + i, "vad_short", speech_ms=120) for i in range(30)])
    first, second = asr_lab.build_queue(records), asr_lab.build_queue(records)
    assert [g["ids"] for g in first["groups"]] == [g["ids"] for g in second["groups"]]
    sizes = {g["key"]: len(g["ids"]) for g in first["groups"]}
    assert sizes == {"A": 5, "B": 15, "C": 10, "D": 20}
    assert f"{10:032x}" not in first["groups"][0]["ids"], "уже розмічене не потрапляє в чергу"
    assert not any(f"{200 + i:032x}" in first["groups"][3]["ids"] for i in range(30)), "клацання VAD не в черзі"


def _tone_noise_tone(sr=16000):
    t = np.arange(int(0.9 * sr)) / sr
    tone = 0.3 * np.sin(2 * np.pi * 400 * t)
    noise = np.random.default_rng(1).normal(0, 0.002, int(1.2 * sr))
    silence = np.zeros(int(0.3 * sr))
    audio = np.concatenate((silence, tone, noise, tone, silence))
    return (audio * 32767).astype("<i2").tobytes()


def test_speech_gate_closes_segment_on_noise_that_vad_calls_speech(monkeypatch):
    from core.vad import VADSegmenter

    class AlwaysSpeech:
        def __init__(self, level):
            pass

        def is_speech(self, frame, sr):
            return True  # як WebRTC VAD на шумі ефіру

    monkeypatch.setattr("core.vad.webrtcvad.Vad", AlwaysSpeech)
    loud = b"\x10\x27" * 480
    quiet = b"\x01\x00" * 480
    frames = [loud] * 25 + [quiet] * 30 + [loud] * 25 + [quiet] * 30
    plain = VADSegmenter(16000, 30, 2, 500, 600, 500, 20)
    gated = VADSegmenter(16000, 30, 2, 500, 600, 500, 20, speech_gate=lambda f: f is loud)
    assert sum(plain.process(f) is not None for f in frames) == 0, "без умови шум тримає сегмент відкритим"
    assert sum(gated.process(f) is not None for f in frames) == 2


def test_lab_resegments_a_merged_record_into_parts(monkeypatch):
    class AlwaysSpeech:
        def __init__(self, level):
            pass

        def is_speech(self, frame, sr):
            return True

    monkeypatch.setattr("core.vad.webrtcvad.Vad", AlwaysSpeech)
    lab = asr_lab.Lab.__new__(asr_lab.Lab)
    lab.args = type("Args", (), {"gate_db": 12.0})()
    pieces = lab.resegment(_tone_noise_tone())
    assert len(pieces) == 2


def test_room_noise_and_multiple_messages_are_reported_separately():
    results = [result("speech", "вишня 210 прийом клен 300 прийом", "вишня 210 прийом", multiple_messages=1),
               result("speech", "клен 300", "клен 300", multiple_messages=0),
               result("room", "", "дякую за перегляд"),
               result("noise", "", "")]
    summary = asr_lab.summarize(results)
    assert summary["room_records"] == 1 and summary["false_text_on_room_noise"] == 1
    assert summary["non_speech_records"] == 1 and summary["false_text_on_non_speech"] == 0, "кімната не змішується з ефіром"
    parts = asr_lab.breakdown(results)
    assert parts["передач у записі: кілька"]["records"] == 1 and parts["передач у записі: одна"]["wer"] == 0
