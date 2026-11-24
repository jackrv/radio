"""Акустичні вимірювання — тільки синтетичні PCM й тимчасовий індекс."""
import json
import sqlite3
import wave

import numpy as np
import pytest

from core.audio_text_consistency import capacity_checks, measure_audio, word_count
from tools.audio_text_audit import audit


def pcm(x):
    return np.rint(np.clip(x, -.999, .999) * 32768).astype('<i2').tobytes()


def signal(sr=16000, seconds=2):
    noise = np.random.default_rng(345).normal(0, .001, sr * seconds)
    start, end = sr // 2, sr * 9 // 10
    noise[start:end] += .08 * np.sin(2 * np.pi * 160 * np.arange(end - start) / sr)
    return noise


def test_silence_and_short_or_empty_audio_are_not_evidence_of_word_boundaries():
    assert measure_audio(b'', 16000)['available'] is False
    short = measure_audio(b'\0\0' * 159, 16000)
    assert short['tail_s'] == pytest.approx(159 / 16000)
    assert capacity_checks(20, short) == {}
    metrics = measure_audio(b'\0\0' * 16000, 16000)
    assert all(value == 0 for value in metrics['activity_s'].values())
    assert all(value == 0 for value in metrics['periodic_s'].values())
    assert capacity_checks(20, metrics)['energy_6']['exceeds'] is True
    assert capacity_checks(1, metrics)['energy_6']['exceeds'] is False
    json.dumps(metrics, allow_nan=False)


@pytest.mark.parametrize('sr', [8000, 16000, 32000, 48000])
def test_activity_and_periodicity_describe_burst_not_whole_recording(sr):
    raw = bytearray(pcm(signal(sr)))
    before = bytes(raw)
    metrics = measure_audio(raw, sr)
    assert bytes(raw) == before
    assert metrics['duration_s'] == 2
    assert metrics['analysed_s'] + metrics['tail_s'] == pytest.approx(2)
    assert .3 < metrics['activity_s']['6'] < .5
    assert .3 < metrics['periodic_s']['0.3'] < .5
    assert metrics['activity_s']['15'] <= metrics['activity_s']['6']
    assert set(metrics).isdisjoint({'text', 'raw_text', 'audio', 'spectrum'})
    json.dumps(metrics, allow_nan=False)


def test_gain_and_large_fft_batches_do_not_turn_proxy_into_word_counter():
    x = signal(seconds=12)
    a, b = measure_audio(pcm(x / 2), 16000), measure_audio(pcm(x * 2), 16000)
    for db in ('6', '12', '15'):
        assert a['activity_s'][db] == pytest.approx(b['activity_s'][db], abs=.03)
    assert a['periodic_s']['0.3'] == pytest.approx(b['periodic_s']['0.3'], abs=.03)
    assert a['flatness_median'] == pytest.approx(b['flatness_median'], rel=.02)
    assert a['floor_rms'] * 4 == pytest.approx(b['floor_rms'], rel=.01)


@pytest.mark.parametrize('sample_rate', [0, -1, 16000.0, True, 44100])
def test_invalid_audio_format_is_explicit(sample_rate):
    with pytest.raises(ValueError):
        measure_audio(b'\0\0', sample_rate)


def test_odd_pcm_and_invalid_budget_are_explicit():
    with pytest.raises(ValueError):
        measure_audio(b'\0', 16000)
    for options in ({'max_words_per_s': float('nan')}, {'max_words_per_s': True},
                    {'extra_words': -1}, {'min_words': 0}):
        with pytest.raises(ValueError):
            capacity_checks(12, {}, **options)
    assert word_count("Клен, п'ять, 42 42") == 4


def archive(tmp_path):
    with sqlite3.connect(tmp_path / 'index.sqlite3') as db:
        db.execute('CREATE TABLE recordings (id TEXT,created REAL,label TEXT,reference TEXT,status TEXT,data TEXT)')
        for i, label in enumerate(('speech', 'room', 'unclear')):
            rid = f'{i:032x}'
            db.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?)',
                       (rid, 1200 * i, label, 'Клен 12' if label == 'speech' else '', 'ready',
                        json.dumps({'raw_text': 'Клен ' * 20})))
            with wave.open(str(tmp_path / (rid + '.wav')), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\0' * 16000)


def test_audit_readonly_reports_losses_and_negative_candidates_without_content(tmp_path):
    archive(tmp_path)
    data_before = (tmp_path / 'index.sqlite3').read_bytes()
    result = audit(tmp_path)
    assert result['mode'] == 'analysis_only'
    assert result['records'] == 3 and result['failures'] == {}
    candidate = result['comparisons']['energy_6']['all']
    assert candidate == {'flagged': {'speech': 1, 'room': 1, 'unclear': 1}, 'ready_negative': 1}
    assert 'Клен' not in json.dumps(result) and 'raw_text' not in json.dumps(result)
    assert (tmp_path / 'index.sqlite3').read_bytes() == data_before
    single = audit(tmp_path, record_id='0' * 32)
    assert single['record']['words'] == 20
    with pytest.raises(LookupError):
        audit(tmp_path, record_id='a' * 32)
    with pytest.raises(ValueError):
        audit(tmp_path, record_id='../private')


def test_missing_or_corrupt_wav_is_counted_not_classified_as_silence(tmp_path):
    archive(tmp_path)
    (tmp_path / ('0' * 32 + '.wav')).unlink()
    (tmp_path / ('1'.zfill(32) + '.wav')).write_bytes(b'not a wav')
    result = audit(tmp_path)
    assert result['records'] == 1 and sum(result['failures'].values()) == 2
    with pytest.raises(ValueError):
        audit(tmp_path, record_id='0' * 32)


def test_silence_alignment_recipe_is_opt_in_and_parses_both_whisper_options():
    from services.lab_queue import BASE, PRESETS
    from lab.asr_lab import parse_whisper_options
    args = PRESETS['word-silence']['args']
    options = [args[i + 1] for i, value in enumerate(args[:-1]) if value == '--whisper-option']
    assert parse_whisper_options(options) == {'word_timestamps': True, 'hallucination_silence_threshold': 2}
    assert '--whisper-option' not in BASE
