"""Очищення лише тимчасових корпусів: план, резервна копія, race і rollback."""
from contextlib import closing
import sqlite3

import pytest

from shared.recordings import RecordingStore
from shared.sqlite_connection import get_db, read_only
from tools.archive_cleanup import prepare, apply, clean_primary, discard_audio_backup


@pytest.fixture
def corpus(tmp_path):
    events = tmp_path / 'events.db'
    with get_db(events) as db:
        db.execute('CREATE TABLE radio_events(id INTEGER PRIMARY KEY,text TEXT,raw_text TEXT)')
        db.executemany('INSERT INTO radio_events VALUES(?,?,?)', [(i,f'synthetic {i}',f'raw {i}') for i in range(1,7)])
        db.execute('CREATE TABLE radio_event_sequence(id INTEGER PRIMARY KEY,next_id INTEGER)')
        db.execute('INSERT INTO radio_event_sequence VALUES(1,99)')
        db.execute('CREATE TABLE telegram_recent_phrases(token TEXT PRIMARY KEY, original_text TEXT,current_text TEXT,created_at INTEGER)')
        db.execute('INSERT INTO telegram_recent_phrases VALUES(?,?,?,?)',('a'*32,'raw','raw',1))
        db.execute('CREATE TABLE correction_sources(fix_id TEXT PRIMARY KEY,revision INTEGER)')
        db.execute("INSERT INTO correction_sources VALUES('db:6',1)")
        db.execute('CREATE TABLE word_corrections(value TEXT)')
        db.execute("INSERT INTO word_corrections VALUES('preserved rule')")
        db.execute('CREATE TABLE telegram_users(value TEXT)')
        db.execute("INSERT INTO telegram_users VALUES('preserved admin')")
    store = RecordingStore(tmp_path / 'archive')

    def save(status='ready',fix=None,label=None):
        data={'status':status,'raw_text':'синтетичний текст'}
        if fix:data['fix_id']=fix
        rid=store.save(b'\0\0'*48000,16000,data)
        if label:store.review(rid,'синтетичний еталон',label,0)
        return rid
    ids={'pending':save(fix='db:1'), 'filtered':save('low_rms',label='speech'),
         'kept':save(fix='db:3',label='speech'), 'shared_pending':save(fix='db:4'),
         'shared_kept':save(fix='db:4',label='speech'), 'mem':save(fix='mem:'+'a'*32),
         'deferred':save('deferred')}
    # Відсіяне не має radio fix, інакше за правилами UI це категорія «Ефір».
    return store,events,ids,save


def remaining(store):
    with store.connect() as db:return {r[0] for r in db.execute('SELECT id FROM recordings')}


def test_cleanup_frozen_scope_backups_and_primary_preserve_rules_and_sequence(corpus,tmp_path):
    store,events,ids,save=corpus
    backup=prepare(store.root,events,tmp_path/'backups',include_reviewed_filtered=True,include_orphan_uncorrected=True)
    new=save()
    with get_db(events) as db:db.execute("INSERT INTO radio_events VALUES(7,'new','new')")
    result=apply(store.root,backup,batch_size=2)
    assert result['removed']==5
    assert remaining(store)=={ids['kept'],ids['shared_kept'],new}
    assert not (store.root/(ids['pending']+'.wav')).exists()
    assert (backup/'audio'/(ids['pending']+'.wav')).read_bytes().startswith(b'RIFF')
    primary=clean_primary(store.root,backup)
    assert primary['removed_events']==3  # db:1 та незв'язані невиправлені db:2/db:5
    assert primary['removed_memory_phrases']==1
    with get_db(events) as db:
        assert db.execute('SELECT id FROM radio_events ORDER BY id').fetchall()==[(3,),(4,),(6,),(7,)]
        assert db.execute('SELECT next_id FROM radio_event_sequence').fetchone()[0]==99
        assert db.execute('SELECT value FROM word_corrections').fetchone()[0]=='preserved rule'
        assert db.execute('SELECT value FROM telegram_users').fetchone()[0]=='preserved admin'
    with closing(read_only(backup/'events.db')) as db:assert db.execute('SELECT count(*) FROM radio_events').fetchone()[0]==6
    with closing(read_only(backup/'index.sqlite3')) as db:assert db.execute('SELECT count(*) FROM recordings').fetchone()[0]==7


def test_default_preserves_all_reviewed_material(corpus,tmp_path):
    store,events,ids,_=corpus
    backup=prepare(store.root,events,tmp_path/'backups')
    apply(store.root,backup)
    assert ids['filtered'] in remaining(store)
    assert ids['kept'] in remaining(store)


def test_concurrent_annotation_and_primary_edit_are_not_deleted(corpus,tmp_path):
    store,events,ids,save=corpus
    backup=prepare(store.root,events,tmp_path/'backups',include_reviewed_filtered=True,include_orphan_uncorrected=True)
    store.review(ids['pending'],'новий еталон','speech',0)
    with get_db(events) as db:db.execute("UPDATE radio_events SET text='new correction' WHERE id=5")
    result=apply(store.root,backup)
    assert ids['pending'] in remaining(store)
    assert result['not_removed_from_plan']==1
    counts=clean_primary(store.root,backup)
    assert counts['preserved_changed_primary']==1
    with get_db(events) as db:
        assert db.execute('SELECT text FROM radio_events WHERE id=5').fetchone()[0]=='new correction'
        assert db.execute('SELECT id FROM radio_events WHERE id=1').fetchone()


def test_incomplete_backup_aborts_before_deletion(corpus,tmp_path):
    store,events,ids,_=corpus
    backup=prepare(store.root,events,tmp_path/'backups')
    (backup/'audio'/(ids['pending']+'.wav')).unlink()
    with pytest.raises(ValueError,match='неповна'):apply(store.root,backup)
    assert len(remaining(store))==7
    assert (store.root/(ids['pending']+'.wav')).exists()


def test_failed_archive_transaction_keeps_rows_and_audio(corpus,tmp_path):
    store,events,ids,_=corpus
    backup=prepare(store.root,events,tmp_path/'backups')
    with store.connect() as db:
        db.execute("CREATE TRIGGER synthetic_fail BEFORE DELETE ON recordings BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):apply(store.root,backup)
    assert len(remaining(store))==7
    assert (store.root/(ids['pending']+'.wav')).exists()


def test_failed_primary_transaction_rolls_back(corpus,tmp_path):
    store,events,ids,_=corpus
    backup=prepare(store.root,events,tmp_path/'backups',include_orphan_uncorrected=True)
    apply(store.root,backup)
    with get_db(events) as db:
        db.execute("CREATE TRIGGER synthetic_fail BEFORE DELETE ON radio_events BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):clean_primary(store.root,backup)
    with get_db(events) as db:assert db.execute('SELECT count(*) FROM radio_events').fetchone()[0]==6


def test_explicit_no_audio_backup_removes_only_copy_before_permanent_cleanup(corpus,tmp_path):
    store,events,ids,_=corpus
    backup=prepare(store.root,events,tmp_path/'backups')
    assert discard_audio_backup(backup)==4
    assert not (backup/'audio').exists()
    assert (store.root/(ids['pending']+'.wav')).exists()
    result=apply(store.root,backup)
    assert result['removed']==4
    assert not (store.root/(ids['pending']+'.wav')).exists()
    assert (store.root/(ids['kept']+'.wav')).exists()
    assert (backup/'index.sqlite3').exists()
