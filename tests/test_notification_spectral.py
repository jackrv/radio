"""Спектральний фільтр сповіщень: синтетичні звуки, тимчасові каталоги, без ефіру."""

from unittest.mock import Mock

import numpy as np
import pytest

from core.diagnostics import RuntimeDiagnostics
from core.notification_spectral import MAX_SCORE_S, SpectralNotificationMatcher
from core.transcriber import Transcriber
from services import notification_eval
from tests.test_notification_filter import SR, pcm, phone_sound, segment, voice_like
from tests.test_local_dashboard import wav


@pytest.fixture
def matcher():
    return SpectralNotificationMatcher([phone_sound()], SR)


def tone(first, second):
    t = np.arange(round(0.7 * SR)) / SR
    frequency = np.where(t < 0.3, first, second)
    return 0.25 * np.exp(-3 * t) * np.sin(2 * np.pi * np.cumsum(frequency) / SR)


@pytest.mark.parametrize("gain", [0.05, 0.3, 1.0])
@pytest.mark.parametrize("noise", [0.0, 0.001])
def test_isolated_sound_matches_regardless_of_volume_and_hiss(matcher, gain, noise):
    audio = segment(phone_sound() * gain) + np.random.default_rng(3).normal(0, noise, len(segment()))
    result = matcher.score(audio)
    assert result["score"] > 0.9 and result["candidate"]
    assert result["offset_s"] == pytest.approx(0.4, abs=0.03)


@pytest.mark.parametrize("noise", [0.0, 0.001])
def test_room_reverb_still_matches(matcher, noise):
    # Реверберація кімнати RT60 ≈ 0,4 с: телефон грає поруч із мікрофоном.
    rng = np.random.default_rng(6)
    t = np.arange(round(0.4 * SR)) / SR
    impulse = rng.normal(0, 1, len(t)) * 10 ** (-3 * t / 0.4) * 0.08
    impulse[0] = 1
    audio = np.convolve(segment(), impulse)[:len(segment())] + rng.normal(0, noise, len(segment()))
    result = matcher.score(audio)
    assert result["score"] > 0.85 and result["candidate"], result


def test_speech_before_or_over_the_sound_is_never_a_candidate(matcher):
    before = np.concatenate((np.zeros(4000), voice_like(0.8) * 0.1, phone_sound(), np.zeros(8000)))
    result = matcher.score(before)
    assert result["score"] > 0.9, "сам звук знайдено"
    assert result["outside_s"] > 0.5 and not result["candidate"]

    over = segment()
    start = round(0.4 * SR) + 800
    voice = voice_like(0.6) * 0.1
    over[start:start + len(voice)] += voice
    result = matcher.score(over)
    assert result["extra_ratio"] > 0.02 and not result["candidate"]


@pytest.mark.parametrize("sound", [tone(1500, 2000), tone(980, 730), np.random.default_rng(4).normal(0, 0.1, 11200)])
def test_other_sounds_are_not_candidates(matcher, sound):
    assert not matcher.score(segment(sound))["candidate"]


def test_no_templates_long_or_odd_segments_are_not_scored(matcher):
    assert SpectralNotificationMatcher([], SR).score(segment()) is None
    assert matcher.score(np.zeros(round((MAX_SCORE_S + 0.1) * SR))) is None
    assert matcher.score_pcm(b"\x00") is None
    assert matcher.score(segment()[:800]) is None, "коротший за зразок"


def test_excluded_template_is_ignored(matcher):
    digest = matcher.templates[0].digest
    assert matcher.score(segment(), exclude={digest}) is None


def test_thresholds_are_configurable():
    strict = SpectralNotificationMatcher([phone_sound()], SR, threshold=0.999, max_outside_s=0.0, max_extra=0.0)
    noisy = segment() + np.random.default_rng(5).normal(0, 0.003, len(segment()))
    result = strict.score(noisy)
    assert result["score"] > 0.9 and not result["candidate"]


def make_worker(mode, matcher):
    recognizer = Mock()
    recognizer.extract_text.return_value = "Вишня 700"
    processor = Mock()
    worker = Transcriber(segments=Mock(), recognizer=recognizer, process_text=lambda v: v, event_processor=processor,
                         sample_rate=SR, min_rms=0.0001, normalize_audio=True, debug_rms=False, blacklist=(),
                         log=Mock(), spectral_matcher=matcher, spectral_mode=mode)
    return worker, recognizer


def test_shadow_mode_scores_but_never_drops(matcher):
    diagnostics = RuntimeDiagnostics()
    worker, recognizer = make_worker("shadow", matcher)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.transcriber.runtime", diagnostics)
        worker.process_segment(pcm(segment()))
    recognizer.transcribe.assert_called_once()
    snapshot = diagnostics.snapshot()
    assert snapshot["counters"]["asr.spectral.scored"] == 1
    assert snapshot["counters"]["asr.spectral.candidate.ready"] == 1
    assert sum(snapshot["notification_spectral"]["buckets"]) == 1
    row = snapshot["notification_spectral"]["recent"][0]
    assert row["notification_candidate"] is True and row["status"] == "ready"
    assert row["notification_score"] > 0.9
    assert snapshot["asr_latency"]["recent_segments"][0]["notification_score"] == row["notification_score"]


def test_active_mode_drops_only_candidates(matcher):
    diagnostics = RuntimeDiagnostics()
    worker, recognizer = make_worker("active", matcher)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("core.transcriber.runtime", diagnostics)
        worker.process_segment(pcm(segment()))
        recognizer.transcribe.assert_not_called()
        worker.process_segment(pcm(np.concatenate((np.zeros(4000), voice_like(1.5) * 0.2, np.zeros(8000)))))
    recognizer.transcribe.assert_called_once()
    counters = diagnostics.snapshot()["counters"]
    assert counters["asr.spectral.filtered"] == 1 and counters["asr.outcome.notification"] == 1


def test_matcher_error_never_stops_recognition():
    broken = Mock()
    broken.templates = (object(),)
    broken.score_pcm.side_effect = RuntimeError("boom")
    worker, recognizer = make_worker("active", broken)
    worker.process_segment(pcm(segment()))
    recognizer.transcribe.assert_called_once()


def archive_with(tmp_path, clips):
    """Аудіоархів із розміченими записами: [(pcm-масив, мітка або None)]."""
    from services.recordings import RecordingStore
    store = RecordingStore(tmp_path / "recordings")
    ids = []
    for audio, label in clips:
        record = store.save(pcm(audio), SR, {"status": "ready"})
        if label is not None:
            store.review(record, "" if label in ("notification", "noise") else "текст", label, 0)
        ids.append(record)
    return store, ids


def test_evaluation_uses_archive_labels_excludes_self_and_recommends_safe_threshold(tmp_path):
    from services.notification_samples import add_sample
    samples = tmp_path / "samples"
    add_sample(samples, wav(phone_sound()), confirmed_isolated=True)
    rng = np.random.default_rng(9)
    clips = [(segment(phone_sound() * (0.2 + i * 0.2)) + rng.normal(0, 0.0008, len(segment())), "notification") for i in range(3)]
    clips.append((phone_sound(), "notification"))  # той самий звук, що й зразок
    clips += [(np.concatenate((np.zeros(4000), voice_like(1.0 + i * 0.1) * 0.2, np.zeros(6000))), "speech" if i % 3 else "unclear")
              for i in range(10)]
    clips.append((segment(rng.normal(0, 0.05, 6000)), "noise"))
    clips.append((segment(phone_sound()), None))  # не розмічено — не оцінюється
    store, ids = archive_with(tmp_path, clips)
    assert notification_eval.label_counts(store) == {"notification": 4, "speech": 10, "noise": 1}

    report = notification_eval.evaluate(store, samples, threshold=0.85, max_outside_s=0.2, max_extra=0.02)
    by_id = {item["record_id"]: item for item in report["items"]}
    assert ids[-1] not in by_id, "нерозмічений запис не оцінюється"
    assert by_id[ids[3]]["self_template"] is True and by_id[ids[3]]["score"] is None
    assert by_id[ids[0]]["self_template"] is False and by_id[ids[0]]["candidate"]
    summary = report["summary"]
    assert summary["status"] == "ok" and (summary["notifications"], summary["speech"], summary["noise"]) == (3, 10, 1)
    assert summary["recommended"]["speech_dropped"] == 0 and summary["recommended"]["notifications_found"] == 3
    assert 0.7 <= summary["recommended_threshold"] <= 0.9, "середина безпечного діапазону, а не його край"


def test_evaluation_skips_long_records_and_reads_archive_only(tmp_path):
    from services.notification_samples import add_sample
    samples = tmp_path / "samples"
    add_sample(samples, wav(phone_sound()), confirmed_isolated=True)
    store, ids = archive_with(tmp_path, [(voice_like(11) * 0.2, "speech"), (segment(), "notification")])
    with store.connect() as db:
        before = db.execute("SELECT id, revision, label FROM recordings ORDER BY id").fetchall()
    report = notification_eval.evaluate(store, samples, threshold=0.85, max_outside_s=0.2, max_extra=0.02)
    by_id = {item["record_id"]: item for item in report["items"]}
    assert by_id[ids[0]]["score"] is None and "10" in by_id[ids[0]]["error"]
    assert report["summary"]["status"] == "need_more"
    with store.connect() as db:
        assert db.execute("SELECT id, revision, label FROM recordings ORDER BY id").fetchall() == before, "оцінка лише читає"



def test_room_noise_counts_as_harmless_to_drop(tmp_path):
    from services.notification_samples import add_sample
    samples = tmp_path / "samples"
    add_sample(samples, wav(phone_sound()), confirmed_isolated=True)
    store, ids = archive_with(tmp_path, [(segment(), "notification"), (segment(np.zeros(4000)), "room")])
    assert notification_eval.label_counts(store) == {"notification": 1, "speech": 0, "noise": 1}
    report = notification_eval.evaluate(store, samples, threshold=0.85, max_outside_s=0.2, max_extra=0.02)
    assert {item["label"] for item in report["items"]} == {"notification", "room"}
