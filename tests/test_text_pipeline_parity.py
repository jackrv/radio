"""Текст приймача й лабораторії: синтетичні виходи Whisper, без моделей і мережі."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from shared.callsigns import Callsigns, command_dictionary
from shared.diagnostics import RuntimeDiagnostics
from core.event_processor import EventProcessor
from core.transcriber import Transcriber
from lab.asr_lab import Lab


def dictionaries():
    return (Callsigns.from_lines(['береза: 56', 'вишня', 'клен', 'ясень']),
            command_dictionary(['злива 4242']), Callsigns.from_lines([]))


def event_processor(dicts):
    return EventProcessor(*dicts, short_garbage={'ах'}, save_event=Mock(return_value=1),
                          telegram_service=Mock(), fresh=lambda _: True,
                          select_recipients=lambda wants: [], db_path='unused',
                          log_callsigns_only=True, log=Mock())


def worker(raw, dicts, processor, metrics, monkeypatch):
    monkeypatch.setattr('core.transcriber.runtime', metrics)
    monkeypatch.setattr('core.event_processor.runtime', metrics)
    recognizer = Mock()
    recognizer.transcribe.return_value = {'segments':[{'text':raw, 'avg_logprob':0}]}
    recognizer.extract_text.return_value = raw
    return Transcriber(segments=Mock(), recognizer=recognizer, process_text=lambda text:text,
                       event_processor=processor, sample_rate=16000, min_rms=0,
                       normalize_audio=True, debug_rms=False, blacklist=(), log=Mock())


def lab(raw, dicts):
    result = Lab.__new__(Lab)
    result.args = SimpleNamespace(normalize='on', min_rms=None, bandpass='off', seed=345,
                                  resegment='none', variant='pipeline')
    result.sample_rate = 16000
    result.dictionaries = dicts
    result.blacklist = []
    result.short_garbage = {'ах'}
    result.garbage_limits = {}
    result.llm = None
    result.log = Mock()
    result.apply_rules = lambda text: (text, False)
    result.recognizer = Mock()
    result.recognizer.transcribe.return_value = {'segments':[{'text':raw, 'avg_logprob':0}]}
    result.recognizer.extract_text.return_value = raw
    return result


def pcm():
    return np.full(48000, 10000, dtype='<i2').tobytes()


@pytest.mark.parametrize('raw,status', [
    ('Берега 56, посилка отримав, прийом', 'ready'),
    ('Вишня', 'bare_callsign'),
    ('ах', 'short_garbage'),
    ('вишня клен ясень', 'dictionary_echo'),
])
def test_receiver_and_lab_agree_on_final_text_and_rejections(raw, status, monkeypatch):
    dicts = dictionaries()
    processor = event_processor(dicts)
    metrics = RuntimeDiagnostics()
    worker(raw, dicts, processor, metrics, monkeypatch).process_segment(pcm())
    assert metrics.snapshot()['asr_latency']['recent_segments'][-1]['status'] == status
    output, _ = lab(raw, dicts).process(pcm())
    if status == 'ready':
        assert output['stage'] == 'pipeline'
        assert processor.save_event.call_args.args[4] == output['hypothesis']
    else:
        assert output['stage'] == status and output['hypothesis'] == ''
        processor.save_event.assert_not_called()
        assert metrics.snapshot()['asr_latency']['ready'] == {}


def test_event_raw_text_is_whisper_text_without_dictionary_echo(monkeypatch):
    # Повний вихід Whisper лишається в архіві; подія отримує текст без переліку назв,
    # бо її raw_text — «оригінал» для правок оператора й навчання.
    raw = 'Береза 56, прийом. вишня клен ясень'
    dicts = dictionaries()
    processor = event_processor(dicts)
    worker(raw, dicts, processor, RuntimeDiagnostics(), monkeypatch).process_segment(pcm())
    assert 'Береза 56' in processor.save_event.call_args.kwargs['raw_text']
    assert 'ясень' not in processor.save_event.call_args.kwargs['raw_text']
    assert 'ясень' not in processor.save_event.call_args.args[4]


def test_lab_uses_frozen_garbage_limits(monkeypatch):
    raw = 'один два три чотири пʼять шість'
    result = lab(raw, dictionaries())
    result.garbage_limits = dict(min_words=4, min_unique_ratio=0.1, max_words_per_s=1, extra_words=0)
    output, _ = result.process(pcm())
    assert output['stage'] == 'garbage' and output['hypothesis'] == ''
