"""Конкурентне читання архіву й надійність відкладеного запису — лише тимчасові бази."""
from contextlib import contextmanager
import sqlite3
import time
from unittest.mock import Mock

import pytest

from shared.recordings import RecordingArchive, RecordingStore
from tests.test_capture_mode import pcm, worker


def test_lab_reader_does_not_block_audio_writer(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    store.save(pcm(), 16000, {'status':'deferred'})
    reader = sqlite3.connect(store.root / 'index.sqlite3')
    try:
        assert reader.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        reader.execute('BEGIN')
        assert reader.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 1
        store.save(pcm(), 16000, {'status':'deferred'})
        assert reader.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 1
        assert store.listing()['total'] == 2
    finally:
        reader.close()


def test_failed_commit_removes_only_its_unindexed_wave(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path / 'archive')
    original = store.connect
    @contextmanager
    def fail_commit():
        with original() as db:
            yield db
            raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(store, 'connect', fail_commit)
    with pytest.raises(sqlite3.OperationalError):
        store.save(pcm(), 16000, {'status':'deferred'})
    assert list(store.root.glob('*.wav')) == []
    with original() as db:
        assert db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 0


def test_writer_retries_busy_commit_without_duplicates_and_keeps_capture_time(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path / 'archive')
    original = store.connect
    failed = False
    @contextmanager
    def busy_once():
        nonlocal failed
        with original() as db:
            yield db
            if not failed:
                failed = True
                raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(store, 'connect', busy_once)
    log = Mock()
    archive = RecordingArchive(store, {}, log)
    captured = time.time() - 60
    archive.submit(pcm(), 16000, {'status':'deferred', 'captured_at':captured})
    archive.close()
    assert not archive.thread.is_alive()
    page = store.listing()
    assert page['total'] == 1 and len(list(store.root.glob('*.wav'))) == 1
    assert page['items'][0]['created'] == captured
    assert log.warning.call_count == 1 and not log.exception.called


def test_cleanup_failure_does_not_turn_saved_audio_into_a_duplicate(tmp_path, monkeypatch):
    store = RecordingStore(tmp_path / 'archive')
    monkeypatch.setattr(store, 'prune', Mock(side_effect=sqlite3.OperationalError('database is locked')))
    log = Mock(); archive = RecordingArchive(store, {}, log)
    archive.submit(pcm(), 16000, {'status':'deferred'})
    archive.close()
    assert store.listing()['total'] == 1
    assert not log.warning.called and not log.exception.called


def test_capture_summary_and_view_keep_processed_and_legacy_records_visible(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    first = store.save(pcm(), 16000, {'status':'deferred'})
    second = store.save(pcm(), 16000, {'status':'deferred', 'capture_only':True})
    store.save(pcm(), 16000, {'status':'low_rms'})
    store.finish_deferred(first, {'status':'low_rms'})
    summary = store.capture_summary()
    assert (summary['saved'], summary['pending'], summary['processed']) == (2, 1, 1)
    assert summary['audio_seconds'] == 2
    assert {r['id'] for r in store.listing(view='captured')['items']} == {first, second}
    assert store.listing(view='deferred')['total'] == 1


def test_model_state_ack_is_retried_without_unloading_twice():
    report = Mock(side_effect=[sqlite3.OperationalError('database is locked'), None])
    w = worker(on_unloaded=report)
    assert w._set_unloaded(True) is False
    assert w._set_unloaded(True) is False  # повтор обмежений паузою
    w._model_report_retry_at = 0
    assert w._set_unloaded(True) is True
    assert w.recognizer.unloads == 1 and report.call_count == 2


def test_failed_deferred_result_write_stays_pending_for_retry():
    store = Mock()
    store.next_deferred.return_value = ('a'*32, pcm())
    w = worker(deferred=store)
    w.process_segment = Mock(side_effect=sqlite3.OperationalError('database is locked'))
    assert w._process_one_deferred() is True
    store.finish_deferred.assert_not_called()  # не позначаємо це помилкою Whisper
    assert w._dry is False and w._deferred_idle_until > time.monotonic()
