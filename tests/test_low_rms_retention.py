"""Година для нерозміченого low_rms, спільний прогноз і прибирання без нового аудіо."""
import json
import queue
import sqlite3
from unittest.mock import Mock

import pytest

from services.recordings import RecordingStore, RecordingArchive
from services.storage_status import audio_status

NOW=1_800_000_000


def seed(tmp_path, rows, **kwargs):
    store=RecordingStore(tmp_path/'archive',**kwargs)
    ids=[]
    with store.connect() as db:
        for i,(age,status,reference,data) in enumerate(rows):
            rid=f'{i:032x}';ids.append(rid)
            db.execute('INSERT INTO recordings(id,created,status,bytes,data,reference) VALUES(?,?,?,?,?,?)',
                       (rid,NOW-age,status,10,json.dumps(data),reference))
            (store.root/(rid+'.wav')).write_bytes(b'synthetic!')
    return store,ids


def test_hour_boundary_and_protections_match_storage_forecast(tmp_path,monkeypatch):
    store,ids=seed(tmp_path,[
        (3601,'low_rms',None,{}),(3600,'low_rms',None,{}),(3599,'low_rms',None,{}),
        (9*86400,'low_rms','',{}),(7200,'ready',None,{'signal':{'duration_s':.6}}),
        (7200,'blacklist',None,{}),(9*86400,'deferred',None,{}),
        (7200,'low_rms',None,{'fix_id':'db:1'}),(8*86400,'ready',None,{}),
    ])
    preview=audio_status(store,NOW)
    assert preview['due_count']==4 and preview['due_bytes']==40
    assert preview['protected_count']==2
    assert preview['low_rms_retention_seconds']==3600
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW)
    store.prune()
    with store.connect() as db:left={r[0] for r in db.execute('SELECT id FROM recordings')}
    assert left=={ids[i] for i in (2,3,4,5,6)}
    assert {p.stem for p in store.root.glob('*.wav')}==left
    after=audio_status(store,NOW)
    assert after['due_count']==0 and after['next_age_expiry_at']==NOW+1


def test_capture_only_protection_is_also_reflected_in_the_preview(tmp_path):
    store,_=seed(tmp_path,[(9*86400,'deferred',None,{}),(100,'low_rms',None,{})])
    result=audio_status(store,NOW)
    assert result['protected_count']==1 and result['due_count']==0
    assert result['next_age_expiry_at']==NOW+3500
    assert result['unreviewed_by_group']['filtered']['count']==1


def test_shorter_general_retention_still_wins(tmp_path,monkeypatch):
    store,ids=seed(tmp_path,[(2100,'low_rms',None,{})],retention_days=.02)
    assert audio_status(store,NOW)['due_count']==1
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW)
    store.prune()
    assert not (store.root/(ids[0]+'.wav')).exists()


def test_quiet_audio_age_uses_capture_time_and_not_save_time(tmp_path,monkeypatch):
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW)
    store=RecordingStore(tmp_path/'archive')
    rid=store.save(b'\0\0'*1600,16000,{'status':'low_rms','captured_at':NOW-3599})
    assert store.get(rid)['created']==NOW-3599
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW+1)
    store.prune()
    with pytest.raises(LookupError):store.get(rid)


def test_idle_archive_writer_prunes_without_new_recordings(tmp_path,monkeypatch):
    store,ids=seed(tmp_path,[(4000,'low_rms',None,{}),(7200,'ready','еталон',{})])
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW)
    archive=RecordingArchive.__new__(RecordingArchive)
    archive.store=store;archive.log=Mock();archive.pending=Mock()
    archive.pending.get.side_effect=[queue.Empty,None]
    archive._run()
    assert not (store.root/(ids[0]+'.wav')).exists()
    assert (store.root/(ids[1]+'.wav')).exists()
    archive.pending.task_done.assert_called_once()


@pytest.mark.parametrize('error',[sqlite3.OperationalError('synthetic busy'),OSError('synthetic disk'),ValueError('synthetic path')])
def test_idle_cleanup_failure_does_not_kill_the_writer(error):
    archive=RecordingArchive.__new__(RecordingArchive)
    archive.store=Mock();archive.log=Mock();archive.pending=Mock()
    archive.store.prune.side_effect=[error,None]
    archive.pending.get.side_effect=[queue.Empty,queue.Empty,None]
    archive._run()
    assert archive.store.prune.call_count==2
    archive.log.exception.assert_called_once()
    archive.pending.task_done.assert_called_once()


def test_failed_prune_commit_preserves_wav(tmp_path,monkeypatch):
    store,ids=seed(tmp_path,[(4000,'low_rms',None,{})])
    original=store.connect
    from contextlib import contextmanager
    @contextmanager
    def failed_commit():
        with original() as db:
            yield db
            raise sqlite3.OperationalError('synthetic commit failure')
    monkeypatch.setattr(store,'connect',failed_commit)
    monkeypatch.setattr('services.recordings.time.time',lambda:NOW)
    with pytest.raises(sqlite3.OperationalError):store.prune()
    assert (store.root/(ids[0]+'.wav')).exists()
    with original() as db:assert db.execute('SELECT count(*) FROM recordings').fetchone()[0]==1
