"""Синтетичні регресії незалежного аудиту; жодних робочих баз або HTTP."""
import io
import logging
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests


def test_command_header_and_event_do_not_choose_nearest_number_without_group_anchor():
    from core.callsigns import Callsigns, command_dictionary
    from core.event_processor import EventProcessor
    bot, save = Mock(), Mock(return_value=1)
    processor = EventProcessor(callsigns=Callsigns.from_lines([]), commands=command_dictionary(['грім 022']),
        watch=Callsigns.from_lines([]), short_garbage=set(), save_event=save, telegram_service=bot,
        fresh=lambda _: True, select_recipients=lambda _: [{'chat_id': 1, 'role': 'admin', 'mode': 'all'}],
        db_path='unused.db', log_callsigns_only=True, log=Mock())
    processor.process('грім 123', b'\0\0')
    assert not save.called
    assert bot.notify.call_args.args[3] == 'ГРІМ 123'
    assert not bot.notify.call_args.kwargs['commands_str']


@pytest.mark.parametrize('text', ['у 2026 році', 'код1234', 'код022', '12:3456', '12.3456', '2026-11-20', '20-11-2026'])
def test_editor_preserves_context_of_numbers(text):
    from services.text_check import format_text
    assert format_text(text) == text


def test_late_delivery_inherits_deletion_tombstone(tmp_path):
    from services.telegram_recall import request_delete, state, process_deletions
    from services.telegram_sync import remember_delivery
    path = tmp_path / 'events.db'
    request_delete(path, 'db:1')
    remember_delivery(path, 'db:1', {'chat': {'id': 11}, 'message_id': 5}, 'voice')
    assert state(path, ['db:1'])['db:1']['pending'] == 1
    bot = Mock(db_path=path); bot._maintenance_stop = None
    bot._request.return_value = Mock(status_code=200, json=lambda: {'ok': True})
    process_deletions(bot)
    assert state(path, ['db:1'])['db:1']['copies'] == 0


def test_exhausted_deletion_can_be_retried(tmp_path):
    from services.telegram_recall import request_delete, state, process_deletions
    from services.telegram_sync import remember_delivery
    path = tmp_path / 'events.db'
    remember_delivery(path, 'db:1', {'chat': {'id': 11}, 'message_id': 5}, 'voice')
    request_delete(path, 'db:1', now=1)
    bot = Mock(db_path=path); bot._maintenance_stop = None
    bot._request.side_effect = requests.ConnectionError('synthetic')
    for i in range(8): process_deletions(bot, now=2+31*i)
    status = state(path, ['db:1'])['db:1']
    assert status['exhausted'] == 1 and status['pending'] == 0 and status['copies'] == 1
    assert process_deletions(bot, now=9999) == 0
    assert request_delete(path, 'db:1', now=9999) == 1
    bot._request.side_effect = None
    bot._request.return_value = Mock(status_code=200, json=lambda: {'ok': True})
    assert process_deletions(bot, now=10000) == 1
    assert state(path, ['db:1'])['db:1']['copies'] == 0


@pytest.mark.parametrize('payload', [{'ok': False}, [], None])
def test_own_delete_does_not_claim_success(payload):
    from services.telegram_updates import UpdatesMixin
    bot = UpdatesMixin(); bot.db_path = 'unused.db'
    bot._request = Mock(return_value=Mock(status_code=200, json=lambda: payload))
    bot._answer_callback = Mock()
    with patch('services.telegram_updates.forget_delivery') as forget:
        bot._delete_own_copy({'id': 'synthetic'}, 11, {'message_id': 5})
        assert not forget.called
        assert bot._answer_callback.call_args.args[1] == 'Помилка'


def test_files_private_and_transcript_stays_in_console_only(tmp_path):
    from services.sqlite_connection import get_db
    from core.log_security import TechnicalFileFormatter, RedactingFormatter, PrivateRotatingFileHandler
    path = tmp_path / 'events.db'
    old = os.umask(0o022)
    try:
        with get_db(path) as db:
            db.execute('create table synthetic (id integer)')
            for suffix in ['', '-wal', '-shm']:
                assert (tmp_path / ('events.db'+suffix)).stat().st_mode & 0o777 == 0o600
        log = tmp_path / 'radio.log'
        handler = PrivateRotatingFileHandler(log, maxBytes=50, backupCount=1)
        handler.setFormatter(TechnicalFileFormatter('%(message)s', None, 'TOKEN'))
        record = logging.makeLogRecord({'msg':'Розпізнано (початкове): %s', 'args':('SYNTHETIC_TRANSCRIPT',), 'levelno':20})
        handler.emit(record); handler.emit(record); handler.close()
        assert 'SYNTHETIC_TRANSCRIPT' not in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600
        assert 'SYNTHETIC_TRANSCRIPT' in RedactingFormatter('%(message)s', None, '').format(record)
        from core.console import ConsoleDashboard
        stream=io.StringIO();console=ConsoleDashboard(stream=stream)
        console.setFormatter(RedactingFormatter('%(message)s',None,''));console.emit(record)
        assert 'SYNTHETIC_TRANSCRIPT' in stream.getvalue()
    finally: os.umask(old)


@pytest.mark.parametrize('option', ['beam_size=5', 'language=en', 'initial_prompt=x', 'temperature=0', 'unknown=1',
                                  'patience=2','best_of=0','best_of=1.5','word_timestamps=1','logprob_threshold=1e309'])
def test_lab_rejects_unsafe_options(option):
    from lab.asr_lab import parse_whisper_options
    with pytest.raises(SystemExit): parse_whisper_options([option])


@pytest.mark.parametrize('retry', ['on', 'off'])
def test_lab_retry_contract_shared_by_all_segments(retry):
    from lab.asr_lab import Lab
    from core.recognition import WhisperRecognizer
    worker = object.__new__(Lab); worker.args = SimpleNamespace(retry=retry)
    worker.recognizer = SimpleNamespace(prompt='synthetic', extract_text=WhisperRecognizer.extract_text,
        transcribe=Mock(side_effect=[{'segments':[{'text':'loop','compression_ratio':3}]},
                                    {'segments':[{'text':'Recovered','compression_ratio':1}]}]))
    _, text, info = worker._transcribe_with_retry(b'')
    assert info['attempted'] == (retry == 'on')
    assert text == ('Recovered' if retry == 'on' else '')
