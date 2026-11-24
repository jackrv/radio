"""Прогноз очищення — лише тимчасова SQLite й синтетичні файли."""
import hashlib
import sqlite3
from types import SimpleNamespace

from shared.diagnostic_history import DiagnosticsHistory
from shared.recordings import RecordingStore
from server.storage_status import audio_status, log_status, storage_status


def seed(tmp_path, limit=100):
    store = RecordingStore(tmp_path/'recordings', retention_days=7, max_bytes=limit)
    now = 1_800_000_000
    with store.connect() as db:
        for index, (age, size, reference) in enumerate([(10,60,'еталон'),(0,30,None),(1,20,None),(9,5,None)]):
            db.execute('INSERT INTO recordings(id,created,status,bytes,data,reference) VALUES (?,?,?,?,?,?)',
                       (str(index), now-age*86400, 'ready', size, '{}', reference))
            (store.root/f'{index}.wav').write_bytes(b'0'*size)
    return store, now


def test_readonly_projection_matches_actual_prune_and_preserves_reviewed_material(tmp_path):
    store, now = seed(tmp_path)
    index = store.root/'index.sqlite3'
    before = hashlib.sha256(index.read_bytes()).digest()
    status = audio_status(store, now)
    assert status['bytes'] == 115 and status['protected_bytes'] == 60
    assert status['due_count'] == 2 and status['due_bytes'] == 25
    assert status['next_age_expiry_at'] == now-2*86400
    assert status['remaining_bytes'] == 0
    assert hashlib.sha256(index.read_bytes()).digest() == before
    assert all((store.root/f'{i}.wav').exists() for i in range(4))
    from unittest.mock import patch
    with patch('shared.recordings.time.time', return_value=now):
        store.prune()
    after = audio_status(store, now)
    assert after['count'] == 2 and after['bytes'] == 90 and after['protected_count'] == 1


def test_missing_archive_is_not_created_by_status(tmp_path):
    store = RecordingStore(tmp_path/'absent')
    assert audio_status(store, 1_800_000_000)['available'] is False
    assert not store.root.exists()


def test_protected_records_can_exceed_budget_without_becoming_deletion_candidates(tmp_path):
    store, now = seed(tmp_path, limit=50)
    result = audio_status(store, now)
    assert result['protected_bytes'] == 60
    assert result['due_count'] == 3
    assert result['due_bytes'] == 55


def test_logs_rotate_by_size_and_count_without_age_expiry(tmp_path):
    path = tmp_path/'radio.log';path.write_bytes(b'x'*100)
    for i in range(1,6):
        (tmp_path/f'radio-{i}.log').write_bytes(b'y'*10)
    result = log_status(path)
    assert result['bytes'] == 150 and result['archive_count'] == 5
    assert result['rotations_until_oldest_removed'] == 1
    assert result['remaining_to_rotation_bytes'] == result['max_file_bytes']-100
    assert result['max_total_bytes'] == 6*result['max_file_bytes']


def test_status_counts_diagnostics_and_separates_manual_backups(tmp_path):
    store, now = seed(tmp_path)
    history = DiagnosticsHistory(tmp_path/'history');history.directory.mkdir()
    (history.directory/('20261117T120000000000Z-'+'a'*32+'.json')).write_text('{}')
    backups = tmp_path/'backups';backups.mkdir()
    (backups/'events-auto-20261117T120000Z.db').write_bytes(b'a'*10)
    (backups/'before-reset.db').write_bytes(b'b'*20)
    status = storage_status(tmp_path/'events.db',store,history,backups,now=now)
    assert status['diagnostics']['count'] == 1
    assert status['backups']['automatic_bytes'] == 10
    assert status['backups']['manual_bytes'] == 20
    assert status['disk']['free_bytes'] > 0
