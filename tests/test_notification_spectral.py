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


def test_evaluation_excludes_self_and_recommends_safe_threshold(tmp_path):
    from services.notification_samples import add_sample
    samples, labels = tmp_path / "samples", tmp_path / "eval"
    add_sample(samples, wav(phone_sound()), confirmed_isolated=True)
    rng = np.random.default_rng(9)
    for index in range(3):
        noisy = segment(phone_sound() * (0.2 + index * 0.2)) + rng.normal(0, 0.0008, len(segment()))
        notification_eval.set_label(labels, f"db:{index + 1}", "notification", wav(noisy))
    notification_eval.set_label(labels, "db:50", "notification", wav(phone_sound()))  # той самий запис, що й зразок
    for index in range(10):
        clip = np.concatenate((np.zeros(4000), voice_like(1.0 + index * 0.1) * 0.2, np.zeros(6000)))
        notification_eval.set_label(labels, f"db:{index + 100}", "speech", wav(clip))
    assert notification_eval.label_counts(labels) == {"notification": 4, "speech": 10}

    report = notification_eval.evaluate(labels, samples, threshold=0.85, max_outside_s=0.2, max_extra=0.02)
    by_fix = {item["fix_id"]: item for item in report["items"]}
    assert by_fix["db:50"]["self_template"] is True and by_fix["db:50"]["score"] is None
    assert by_fix["db:1"]["self_template"] is False and by_fix["db:1"]["candidate"]
    summary = report["summary"]
    assert summary["status"] == "ok" and summary["notifications"] == 3 and summary["speech"] == 10
    assert summary["recommended"]["speech_dropped"] == 0 and summary["recommended"]["notifications_found"] == 3
    assert 0.7 <= summary["recommended_threshold"] <= 0.9, "середина безпечного діапазону, а не його край"


def test_labels_are_private_bounded_and_replaceable(tmp_path):
    root = tmp_path / "eval"
    notification_eval.set_label(root, "db:1", "speech", wav())
    notification_eval.set_label(root, "db:1", "notification", wav())
    assert notification_eval.read_labels(root) == {"db:1": "notification"}
    assert len(list(root.glob("*.wav"))) == 1
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    with pytest.raises(ValueError):
        notification_eval.set_label(root, "../x", "speech", wav())
    with pytest.raises(ValueError):
        notification_eval.set_label(root, "db:2", "music", wav())
    with pytest.raises(ValueError):
        notification_eval.set_label(root, "db:2", "speech", b"not a wav")
    notification_eval.remove_label(root, "db:1")
    assert notification_eval.read_labels(root) == {} and not list(root.glob("*.wav"))
    (root / "labels.json").write_text("{broken")
    assert notification_eval.read_labels(root) == {}
