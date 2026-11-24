"""Поділ і цілі навчання перевіряються без MLX/робочого архіву."""
from types import SimpleNamespace
import hashlib
import json
import wave
import pytest

from lab import whisper_finetune as ft


def row(split, label='speech', text='Вишня 210, прийом', sha='same', created=100):
    return {'id': f'{split}-{created}', 'split': split, 'label': label,
            'text': text, 'pcm_sha256': sha, 'created': created}


def test_duplicate_audio_cannot_leak_from_test_into_training():
    kept, excluded = ft.separate_duplicates([row('train'), row('test'), row('validation')])
    assert [r['split'] for r in kept] == ['test']
    assert excluded['duplicate_audio'] == 2


def test_conflicting_ground_truth_for_identical_audio_is_excluded():
    kept, excluded = ft.separate_duplicates([row('train'), row('test', text='Інший еталон')])
    assert not kept
    assert excluded['conflicting_audio'] == 2


def test_validation_precedes_train_and_boundary_precedes_train():
    kept, _ = ft.separate_duplicates([row('train'), row('validation')])
    assert kept[0]['split'] == 'validation'
    kept, _ = ft.separate_duplicates([row('train'), row('boundary')])
    assert kept[0]['split'] == 'boundary'


def test_split_never_uses_existing_lab_holdout_for_training():
    base = 1800000000 // ft.SPLIT_BLOCK_S * ft.SPLIT_BLOCK_S
    names = set()
    for block in range(120):
        created = base + block * ft.SPLIT_BLOCK_S + 100
        result = ft.partition(created)
        if ft.split_of(created) == 'holdout':
            assert result == 'test'
        names.add(result)
        assert ft.partition(created + 50) == result
    assert {'train', 'validation', 'test'} <= names


def test_boundary_guard_excludes_only_non_test_blocks():
    block = next(i for i in range(1000) if ft.split_of(i * ft.SPLIT_BLOCK_S) == 'tune')
    assert ft.partition(block * ft.SPLIT_BLOCK_S + 10) == 'boundary'
    assert ft.partition(block * ft.SPLIT_BLOCK_S + 1190) == 'boundary'


@pytest.fixture
def tokenizer():
    return SimpleNamespace(sot=1, no_speech=8, eot=9,
                           sot_sequence_including_notimestamps=(1, 2, 3, 4),
                           encode=lambda text: [5, 6, 7])


def test_speech_teacher_forcing_targets_shift_without_losing_eot(tokenizer):
    inputs, targets = ft.target_sequence(tokenizer, 'Вишня 210', 'speech', 448)
    assert inputs == [1, 2, 3, 4, 5, 6, 7]
    assert targets == [2, 3, 4, 5, 6, 7, 9]


def test_negative_teaches_nospeech_immediately_after_sot(tokenizer):
    assert ft.target_sequence(tokenizer, '', 'noise', 448) == ([1, 8], [8, 9])


def test_long_reference_is_rejected_without_silent_truncation(tokenizer):
    with pytest.raises(ValueError, match='не обрізається'):
        ft.target_sequence(tokenizer, 'Текст', 'speech', 6)


def test_exported_text_preserves_number_groups():
    assert ft.training_text(' Вишня 0 11 / я Вишня 700 | прийом ') == 'Вишня 0 11 я Вишня 700 прийом'


def test_wav_format_validation_uses_actual_samples(tmp_path):
    path = tmp_path / 'sample.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes(b'\x00\x00' * 8000)
    with pytest.raises(ValueError, match='16000'):
        ft.read_pcm(path)


def test_edited_manifest_is_rejected(tmp_path):
    raw = b'[]'
    (tmp_path / 'manifest.json').write_bytes(raw)
    (tmp_path / 'summary.json').write_text(json.dumps({'seed':345,'manifest_sha256':hashlib.sha256(raw).hexdigest()}))
    assert ft.checked_manifest(tmp_path, 345) == []
    (tmp_path / 'manifest.json').write_text('[{}]')
    with pytest.raises(ValueError, match='manifest'):
        ft.checked_manifest(tmp_path, 345)


def test_prepare_copies_only_train_validation_and_keeps_sources(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'; archive.mkdir()
    records = []
    for number in range(1, 70):
        identifier = f'sample-{number}'
        with wave.open(str(archive / (identifier + '.wav')), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(number.to_bytes(2, 'little') * 16000)
        records.append({'id':identifier,'created':1800000000 + number * ft.SPLIT_BLOCK_S + 100,
                        'label':'speech','reference':'Вишня 210, прийом'})
    monkeypatch.setattr(ft, 'load_records', lambda *a, **k: records)
    target = tmp_path / 'dataset'
    summary = ft.prepare(archive, target)
    frozen = ft.checked_manifest(target, 345)
    assert all(summary['splits'][s]['labels']['speech'] > 0 for s in ('train','validation','test'))
    assert len(list(archive.glob('*.wav'))) == len(records)
    for r in frozen:
        if r['split'] == 'test':
            assert r['audio'] is None
        else:
            assert (target / r['audio']).read_bytes() == (archive / (r['id']+'.wav')).read_bytes()
    with pytest.raises(FileExistsError):
        ft.prepare(archive, target)
