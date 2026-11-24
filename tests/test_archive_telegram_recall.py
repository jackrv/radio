"""Немовлення відкликає копії через стійкі черги; лише тимчасові БД й mock HTTP."""
from contextlib import contextmanager
import sqlite3
from unittest.mock import Mock

import pytest

from shared import archive_telegram_recall as recall
from shared.event_store import init_db
from shared.recordings import RecordingStore
from services.telegram import TelegramService
from shared.telegram_recall import MAX_ATTEMPTS, process_deletions, state
from shared.telegram_sync import remember_delivery
from tests.test_local_dashboard import local, ORIGIN  # noqa: F401
from tests.test_miniapp_management import setup  # noqa: F401 — fixture for local


def make_archive(tmp_path, fix='db:1'):
    path = tmp_path / 'events.db'
    init_db(path)
    store = RecordingStore(tmp_path / 'archive')
    rid = store.save(b'\0\0' * 1600, 16000,
                     {'status': 'ready', 'raw_text': 'синтетичний шелест', 'fix_id': fix})
    return path, store, rid


def due(store):
    with store.connect() as db:
        return db.execute('SELECT due_at FROM recording_recalls').fetchone()[0]


def delivery(path, fix='db:1', chat=1001, message=1, kind='voice'):
    remember_delivery(path, fix, {'chat': {'id': chat}, 'message_id': message}, kind)


def bot(path):
    service = TelegramService('test', 16000, path, Mock())
    service._request = Mock(return_value=Mock(status_code=200, json=lambda: {'ok': True}))
    return service


@pytest.mark.parametrize('label', ['noise', 'notification', 'room'])
@pytest.mark.parametrize('fix', ['db:1', 'mem:' + 'a' * 32])
def test_marking_non_speech_recalls_voice_and_text_for_all_recipients(tmp_path, label, fix):
    path, store, rid = make_archive(tmp_path, fix)
    for chat, mid, kind in ((1001, 1, 'voice'), (1001, 2, 'text'), (2002, 1, 'voice')):
        delivery(path, fix, chat, mid, kind)
    store.review(rid, '', label, 0)
    deadline = due(store)
    assert recall.dispatch(path, store, now=deadline - 1) == 0
    assert state(path, [fix])[fix]['hidden'] is False
    # Новий екземпляр відновлює намір після перезапуску.
    assert recall.dispatch(path, RecordingStore(store.root), now=deadline + 1) == 1
    assert state(path, [fix])[fix]['pending'] == 3
    service = bot(path)
    assert process_deletions(service, now=deadline + 2) == 3
    assert all(call.args[1] == 'deleteMessage' for call in service._request.call_args_list)
    assert state(path, [fix])[fix]['copies'] == 0
    assert store.get(rid)['label'] == label and store.audio(rid)
    assert store.get(rid)['data']['raw_text'] == 'синтетичний шелест'
    assert recall.dispatch(path, store, now=deadline + 3) == 0


def test_undo_or_speech_before_dispatch_cancels_recall(tmp_path):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    store.review(rid, '', 'room', 0)
    deadline = due(store)
    store.restore(rid, 1, {'reference': None, 'label': None})
    assert recall.status(store, rid) is None
    assert recall.dispatch(path, store, now=deadline + 1) == 0
    assert state(path, ['db:1'])['db:1']['hidden'] is False
    store.review(rid, '', 'noise', 2)
    store.review(rid, 'Клен 12, прийом', 'speech', 3)
    assert recall.status(store, rid) is None


def test_review_and_recall_intent_commit_together(tmp_path, monkeypatch):
    path, store, rid = make_archive(tmp_path)
    monkeypatch.setattr(recall, 'stage', Mock(side_effect=sqlite3.OperationalError('synthetic')))
    with pytest.raises(sqlite3.OperationalError):
        store.review(rid, '', 'room', 0)
    assert store.get(rid)['revision'] == 0 and store.get(rid)['label'] is None


def test_failed_transfer_is_retried_after_restart(tmp_path, monkeypatch):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    store.review(rid, '', 'room', 0)
    deadline = due(store)
    real = recall.request_delete
    monkeypatch.setattr(recall, 'request_delete', Mock(side_effect=sqlite3.OperationalError('locked')))
    assert recall.dispatch(path, store, now=deadline + 1) == 0
    assert recall.status(store, rid) is not None
    monkeypatch.setattr(recall, 'request_delete', real)
    assert recall.dispatch(path, RecordingStore(store.root), now=deadline + 2) == 1
    assert state(path, ['db:1'])['db:1']['pending'] == 1


def test_failed_archive_ack_does_not_reset_exhausted_delivery_attempts(tmp_path, monkeypatch):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    store.review(rid, '', 'room', 0)
    deadline = due(store)
    original, calls = store.connect, []
    @contextmanager
    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError('synthetic ack failure')
        with original() as db:
            yield db
    monkeypatch.setattr(store, 'connect', flaky)
    assert recall.dispatch(path, store, now=deadline + 1) == 0
    with sqlite3.connect(path) as db:
        db.execute('UPDATE telegram_deliveries SET attempts=?', (MAX_ATTEMPTS,))
    assert recall.dispatch(path, store, now=deadline + 2) == 1
    assert state(path, ['db:1'])['db:1']['exhausted'] == 1
    assert recall.status(store, rid) is None


def test_late_delivery_inherits_recall_even_when_initially_no_copies_existed(tmp_path):
    path, store, rid = make_archive(tmp_path)
    store.review(rid, '', 'room', 0)
    assert recall.dispatch(path, store, now=due(store) + 1) == 1
    delivery(path)
    assert state(path, ['db:1'])['db:1']['pending'] == 1
    process_deletions(bot(path))
    assert state(path, ['db:1'])['db:1']['copies'] == 0


def test_legacy_delivery_address_is_migrated_before_hiding_phrase(tmp_path):
    from shared.telegram_media_store import ensure_table
    from shared.sqlite_connection import get_db
    path, store, rid = make_archive(tmp_path)
    with get_db(path) as db:
        ensure_table(db)
        db.execute("INSERT INTO telegram_phrase_messages VALUES ('db:1','1001',7,'fake','visible')")
    store.review(rid, '', 'room', 0)
    recall.dispatch(path, store, now=due(store) + 1)
    assert state(path, ['db:1'])['db:1']['pending'] == 1
    service = bot(path)
    process_deletions(service)
    assert service._request.call_args.kwargs['data'] == {'chat_id': 1001, 'message_id': 7}


def test_telegram_refusal_is_not_reported_as_successful_deletion(tmp_path):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    store.review(rid, '', 'room', 0)
    recall.dispatch(path, store, now=due(store) + 1)
    service = bot(path)
    service._request.return_value = Mock(status_code=400, json=lambda: {
        'ok': False, 'description': "Bad Request: message can't be deleted"})
    process_deletions(service)
    result = state(path, ['db:1'])['db:1']
    assert result['copies'] == 1 and result['exhausted'] == 1 and result['pending'] == 0
    assert store.get(rid)['label'] == 'room'


def test_expired_mem_text_and_list_cleanup_do_not_erase_pending_recall(tmp_path):
    from shared.telegram_phrase_store import register_phrase
    from shared.telegram_sync import sync_corrections
    from services.management.phrases import list_phrases
    fix = 'mem:' + 'b' * 32
    path, store, rid = make_archive(tmp_path, fix)
    register_phrase(path, 'b' * 32, 'синтетичний шелест')
    delivery(path, fix)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE telegram_recent_phrases SET created_at=0')
    store.review(rid, '', 'room', 0)
    deadline = due(store)
    recall.dispatch(path, store, now=deadline + 1)
    service = bot(path)
    sync_corrections(service, now=deadline + 2)
    list_phrases(path)
    assert state(path, [fix])[fix]['pending'] == 1
    # Пізня копія успадковує tombstone навіть після очищення тексту й журналу.
    delivery(path, fix, chat=2002)
    assert state(path, [fix])[fix]['pending'] == 2
    process_deletions(service, now=deadline + 3)
    assert state(path, [fix])[fix]['copies'] == 0


def test_expired_mem_keeps_recent_address_without_editing_or_restoring_text(tmp_path):
    from shared.telegram_phrase_store import register_phrase, load_phrase
    from shared.telegram_sync import queue_correction, sync_corrections
    from shared.sqlite_connection import get_db
    fix = 'mem:' + 'c' * 32
    path, store, rid = make_archive(tmp_path, fix)
    register_phrase(path, 'c' * 32, 'синтетичний шелест')
    delivery(path, fix)
    with get_db(path) as db:
        queue_correction(db, fix)
        db.execute('UPDATE telegram_recent_phrases SET created_at=0')
    service = bot(path)
    assert sync_corrections(service) == 0
    service._request.assert_not_called()
    assert load_phrase(path, 'c' * 32) is None
    assert state(path, [fix])[fix]['copies'] == 1


def test_legacy_labels_and_records_without_link_are_not_mass_recalled(tmp_path):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    with store.connect() as db:
        db.execute("UPDATE recordings SET label='room',reference='' WHERE id=?", (rid,))
    assert recall.dispatch(path, store) == 0
    assert not state(path, ['db:1'])['db:1']['hidden']
    unlinked = store.save(b'\0\0' * 1600, 16000, {'status': 'ready', 'raw_text': 'шум'})
    store.review(unlinked, '', 'room', 0)
    assert recall.status(store, unlinked) is None
    store.review(rid, '', 'room', 0)
    with pytest.raises(ValueError):
        store.review(rid, '', 'noise', 0)
    assert recall.status(store, rid) is not None


def test_bot_maintenance_dispatches_due_archive_intents(tmp_path, monkeypatch):
    path, store, rid = make_archive(tmp_path)
    delivery(path)
    store.review(rid, '', 'room', 0)
    deadline = due(store)
    service = bot(path)
    service.archive_root = store.root
    monkeypatch.setattr(recall.time, 'time', lambda: deadline + 1)
    service.maintenance_once()
    assert state(path, ['db:1'])['db:1']['copies'] == 0


def test_api_review_reports_scheduled_recall_and_requires_origin(local):
    client, path, tmp = local
    store = RecordingStore(tmp / 'recordings')
    rid = store.save(b'\0\0' * 1600, 16000, {'status': 'ready', 'raw_text': 'шум', 'fix_id': 'db:9'})
    delivery(path, 'db:9')
    url = f'/api/recordings/{rid}/review'
    payload = {'reference': '', 'label': 'room', 'expected_revision': 0}
    assert client.put(url, json=payload).status_code == 403
    assert store.get(rid)['label'] is None
    result = client.put(url, json=payload, headers={'Origin': ORIGIN})
    assert result.status_code == 200, result.text
    assert result.json()['telegram_recall']['status'] == 'scheduled'
    assert result.json()['telegram_recall']['after_seconds'] > 15
    assert not state(path, ['db:9'])['db:9']['requested']
    assert client.put(url, json=payload, headers={'Origin': ORIGIN}).status_code == 409
    restored = client.post(f'/api/recordings/{rid}/restore', headers={'Origin': ORIGIN},
                          json={'expected_revision': 1, 'label': None, 'reference': None})
    assert restored.status_code == 200, restored.text
    assert recall.status(store, rid) is None
