import io
import json
import logging
import wave
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from services.recordings import RecordingStore, RecordingArchive, scores
from api.local_dashboard import create_dashboard
from tests.test_miniapp_management import setup


def pcm(amplitude=2000):
    return np.full(16000, amplitude, dtype=np.int16).tobytes()


def test_audio_text_metrics_review_and_conflict(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    record_id = store.save(pcm(), 16000, {'status':'ready','raw_text':'одне слово','final_text':'два слова'})
    with wave.open(io.BytesIO(store.audio(record_id))) as wav:
        assert wav.getframerate() == 16000
        assert wav.readframes(16000) == pcm()
    item = store.get(record_id)
    assert item['data']['signal']['duration_s'] == 1
    assert item['data']['signal']['clipping_ratio'] == 0
    assert item['reference'] is None
    reviewed = store.review(record_id, 'одне слово', 'speech', 0)
    assert reviewed['quality']['raw_text']['wer'] == 0
    assert reviewed['quality']['final_text']['wer'] == 1
    assert reviewed['data'] == item['data']
    with pytest.raises(ValueError):
        store.review(record_id, 'перезапис', 'speech', 0)
    assert store.summary()['quality']['raw_text']['wer'] == 0
    assert store.listing(review='reviewed')['total'] == 1
    assert store.listing(review='pending')['total'] == 0
    assert (store.root / (record_id + '.wav')).stat().st_mode & 0o777 == 0o600
    for value in ('../index', 'a'*31, '../../etc/passwd'):
        with pytest.raises(LookupError):
            store.audio(value)


def test_retention_protects_reference_corpus(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    kept = store.save(pcm(),16000,{'status':'ready'})
    store.review(kept,'фраза','speech',0)
    removed = store.save(pcm(),16000,{'status':'low_rms'})
    store.max_bytes = 1
    store.prune()
    assert store.get(kept)['reference'] == 'фраза'
    assert store.audio(kept)
    with pytest.raises(LookupError):
        store.get(removed)
    assert not (store.root / (removed+'.wav')).exists()


def test_writer_handles_disk_failure_and_flushes(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    archive = RecordingArchive(store, {'LANGUAGE':'uk'}, Mock())
    archive.submit(pcm(), 16000, {'status':'low_rms'})
    archive.close()
    assert store.listing()['items'][0]['data']['settings'] == {'LANGUAGE':'uk'}
    broken = Mock(); broken.save.side_effect=OSError('disk full')
    log = Mock()
    archive = RecordingArchive(broken,{},log)
    archive.submit(pcm(),16000,{'status':'low_rms'})
    archive.close()
    assert log.exception.called
    assert not archive.thread.is_alive()


def test_scores_do_not_treat_silence_as_perfect_transcription():
    assert scores('', 'вигаданий текст')['wer'] is None
    assert scores('номер двадцять три','номер 23')['number_match']
    assert scores('номер 23','номер 24')['number_match'] is False


def test_recordings_are_local_authenticated_and_reference_only(setup,tmp_path):
    public, db = setup
    root=tmp_path/'archive'
    store=RecordingStore(root)
    record_id=store.save(pcm(),16000,{'status':'ready','raw_text':'слово','final_text':'слова'})
    app=create_dashboard(db,'x'*43,recordings_dir=root)
    with TestClient(app,base_url='http://127.0.0.1:8766',client=('127.0.0.1',1234)) as client:
        assert client.get('/api/recordings').status_code==401
        origin={'Origin':'http://127.0.0.1:8766'}
        assert client.post('/session',json={'token':'x'*43},headers=origin).status_code==204
        assert client.get('/api/recordings').json()['total']==1
        audio=client.get(f'/api/recordings/{record_id}/audio')
        assert audio.status_code==200 and audio.content[:4]==b'RIFF'
        payload={'reference':'слово','label':'speech','expected_revision':0}
        assert client.put(f'/api/recordings/{record_id}/review',json=payload).status_code==403
        result=client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin)
        assert result.status_code==200 and result.json()['quality']['raw_text']['wer']==0
        assert client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin).status_code==409
        assert client.get('/api/recordings/summary').json()['reviewed']==1
        export=client.get('/api/recordings/export')
        assert export.json()['items'][0]['reference']=='слово'
    assert public.get('/api/recordings').status_code==404
    from services.sqlite_connection import get_db
    with get_db(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM word_corrections').fetchone()[0]==0


def test_transcriber_records_before_low_rms_and_after_corrections():
    from tests.test_transcriber import make_transcriber, make_pcm
    from core.pipeline import build_text_pipeline
    worker,recognizer,_,_=make_transcriber()
    worker.recorder=Mock()
    worker.process_segment(make_pcm(amplitude=1))
    data=worker.recorder.submit.call_args.args[2]
    assert data['status']=='low_rms'
    recognizer.transcribe.assert_not_called()
    worker.process_text=build_text_pipeline(lambda text: text,lambda text:(text,False),(),lambda *a:True,Mock())
    worker.process_segment(make_pcm())
    data=worker.recorder.submit.call_args.args[2]
    assert data['status']=='ready'
    assert data['raw_text']=='Вишня 700'
    assert data['final_text']=='Вишня 700'
    assert data['llm_accepted'] is True
    assert 'whisper_s' in data


def test_short_vad_audio_is_saved_before_discard():
    from core.vad import VADSegmenter
    archive = Mock()
    segmenter = VADSegmenter(16000, 30, 2, 90, 60, 500, 20, on_discard=archive)
    segmenter.vad = Mock()
    segmenter.vad.is_speech.side_effect = [True, True, False, False]
    frame = b"\0" * 960
    for _ in range(4):
        assert segmenter.process(frame) is None
    archive.assert_called_once_with(frame * 4, 16000, 60)


def test_nonfinite_whisper_scores_are_serializable(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    record_id=store.save(pcm(),16000,{'status':'empty_text','whisper_segments':[{'avg_logprob':float('-inf'),'no_speech_prob':np.float32(0.9)}]})
    assert store.get(record_id)['data']['whisper_segments'][0]['avg_logprob'] is None


def test_role_references_unknown_absent_and_accuracy(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    record_id=store.save(pcm(),16000,{'status':'ready','raw_text':'фраза','final_text':'фраза',
                                      'predicted_recipient':'Віра 68','predicted_sender':''})
    assert store.summary()['roles']['recipient']['accuracy'] is None
    reviewed=store.review(record_id,'фраза','speech',0,reference_recipient=' віра 68 ',reference_sender='')
    assert reviewed['reference_recipient']=='віра 68'
    assert reviewed['role_quality']['recipient']['match'] is True
    assert reviewed['role_quality']['sender']['match'] is True
    assert store.summary()['roles']['recipient']=={'evaluated':1,'correct':1,'accuracy':1}
    # Text-only clients preserve previously labelled roles.
    store.review(record_id,'нова фраза','speech',1)
    assert store.get(record_id)['reference_recipient']=='віра 68'
    # Explicit null means unknown, and must leave the accuracy denominator.
    store.review(record_id,'нова фраза','speech',2,reference_recipient=None,reference_sender='Вовк 3')
    summary=store.summary()
    assert summary['roles']['recipient']['evaluated']==0
    assert summary['roles']['sender']['accuracy']==0
    assert store.get(record_id)['data']['predicted_recipient']=='Віра 68'


def test_old_recording_schema_migrates_without_inventing_predictions(tmp_path):
    import sqlite3
    root=tmp_path/'archive'; root.mkdir()
    with sqlite3.connect(root/'index.sqlite3') as db:
        db.execute('''CREATE TABLE recordings (id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
            bytes INTEGER NOT NULL, data TEXT NOT NULL, reference TEXT,
            label TEXT, revision INTEGER NOT NULL DEFAULT 0, reviewed_at REAL)''')
        db.execute('INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?,?)',
                   ('a'*32,1,'ready',0,json.dumps({'raw_text':'фраза'}),'фраза','speech',2,2))
    store=RecordingStore(root)
    item=store.get('a'*32)
    assert item['reference']=='фраза' and item['revision']==2
    assert item['reference_recipient'] is None
    store.review('a'*32,'фраза','speech',2,reference_recipient='Віра 68')
    # A historical role prediction is unavailable, rather than retrospectively guessed.
    assert store.get('a'*32)['role_quality']['recipient']['match'] is None
    assert store.summary()['roles']['recipient']['evaluated']==0


def test_role_review_api_export_validation_and_no_training(setup,tmp_path):
    _,db=setup
    store=RecordingStore(tmp_path/'archive')
    record_id=store.save(pcm(),16000,{'status':'ready','raw_text':'фраза','final_text':'фраза',
                                     'predicted_recipient':'Віра 68','predicted_sender':'Вовк 3'})
    app=create_dashboard(db,'x'*43,recordings_dir=store.root)
    origin={'Origin':'http://127.0.0.1:8766'}
    with TestClient(app,base_url='http://127.0.0.1:8766',client=('127.0.0.1',1234)) as client:
        client.post('/session',json={'token':'x'*43},headers=origin)
        payload={'reference':'фраза','label':'speech','expected_revision':0,
                 'reference_recipient':'Віра 68','reference_sender':'Вовк 3'}
        response=client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin)
        assert response.status_code==200
        assert response.json()['role_quality']['sender']['match'] is True
        export=client.get('/api/recordings/export').json()['items'][0]
        assert export['reference_recipient']=='Віра 68'
        assert export['reference_sender']=='Вовк 3'
        assert client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin).status_code==409
        payload.update(label='noise',reference='',expected_revision=1)
        assert client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin).status_code==422
        payload.update(reference_recipient='',reference_sender='')
        assert client.put(f'/api/recordings/{record_id}/review',json=payload,headers=origin).status_code==200
    from services.sqlite_connection import get_db
    with get_db(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM word_corrections').fetchone()[0]==0


def test_event_processor_captures_actual_role_prediction():
    from services.recordings import _trace
    from tests.test_event_processor import make_processor
    from callsigns import Callsigns
    processor,_,_,_=make_processor(callsigns=Callsigns.from_lines(['Віра: 68', 'Вовк: 3']))
    trace={}
    token=_trace.set(trace)
    try:
        processor.process('Віра 68 я Вовк 3',pcm())
    finally:
        _trace.reset(token)
    assert trace['predicted_recipient']=='віра 68'
    assert trace['predicted_sender']=='вовк 3'
    assert trace['roles_parsed'] is True


def test_short_groups_partition_archive_before_pagination(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    short=store.save(pcm(),16000,{'status':'ready','raw_text':'коротка фраза'})
    boundary=store.save(pcm()*2,16000,{'status':'ready'})
    normal=store.save(pcm()*3,16000,{'status':'ready','raw_text':'прийом'})
    vad=store.save(pcm()*3,16000,{'status':'vad_short'})
    quiet=store.save(pcm()*3,16000,{'status':'low_rms'})
    blacklisted=store.save(pcm()*3,16000,{'status':'blacklist'})
    page=store.listing(group='short',limit=1)
    assert page['groups']=={'all':6,'short':5,'normal':1,'shadow':0}
    assert page['total']==5 and len(page['items'])==1
    assert {r['id'] for r in store.listing(group='short')['items']}=={short,boundary,vad,quiet,blacklisted}
    assert [r['id'] for r in store.listing(group='normal')['items']]==[normal]
    store.review(short,'коротка фраза','speech',0)
    assert store.listing(review='reviewed',group='short')['groups']=={'all':1,'short':1,'normal':0,'shadow':0}
    assert store.listing(review='pending',group='short')['total']==4
    assert len(store.listing()['items'])==6  # No records were removed.


def test_recording_group_api_validation(setup,tmp_path):
    _,db=setup
    store=RecordingStore(tmp_path/'archive')
    store.save(pcm(),16000,{'status':'ready'})
    normal=store.save(pcm()*3,16000,{'status':'ready','text_filter_shadow':{'evaluated':True,'additional':True}})
    app=create_dashboard(db,'x'*43,recordings_dir=store.root)
    with TestClient(app,base_url='http://127.0.0.1:8766',client=('127.0.0.1',1234)) as client:
        client.post('/session',json={'token':'x'*43},headers={'Origin':'http://127.0.0.1:8766'})
        page=client.get('/api/recordings?group=normal').json()
        assert page['total']==1 and page['items'][0]['id']==normal
        assert client.get('/api/recordings?group=short').json()['total']==1
        shadow=client.get('/api/recordings?group=shadow').json()
        assert shadow['total']==1 and shadow['items'][0]['id']==normal
        assert client.get('/api/recordings?group=invalid').status_code==422
        assert client.get('/api/recordings').json()['total']==2


def test_shadow_review_selection_metrics_and_legacy_records(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    candidates={label:store.save(pcm(),16000,{'status':'ready','raw_text':'дякую',
        'text_filter_shadow':{'evaluated':True,'would_filter':True,'additional':True,'phrase':'дякую'}})
        for label in ('speech','noise','notification','room','unclear')}
    store.save(pcm(),16000,{'status':'ready','text_filter_shadow':{'evaluated':True,'additional':False}})
    legacy=store.save(pcm(),16000,{'status':'ready','raw_text':'старий запис'})
    store.review(legacy,'старий запис','speech',0)
    page=store.listing(group='shadow',limit=1)
    assert page['total']==5 and len(page['items'])==1
    assert page['groups']['all']==7 and page['groups']['shadow']==5
    assert {r['id'] for r in store.listing(group='shadow')['items']}==set(candidates.values())
    for label,record_id in candidates.items():
        if label!='unclear':
            store.review(record_id,'дякую' if label=='speech' else '',label,0)
    assert store.listing(group='shadow',review='pending')['total']==1
    assert store.listing(group='shadow',review='reviewed')['groups']['shadow']==4
    assert store.summary()['text_filter_shadow']=={
        'evaluated':6,'candidates':5,'reviewed':4,'speech':1,'noise':1,'notification':1,'room':1,'unclear':0}
    store.review(candidates['unclear'],'','unclear',0)
    assert store.summary()['text_filter_shadow']['unclear']==1
    assert store.get(candidates['speech'])['data']['raw_text']=='дякую'
    assert store.audio(candidates['speech'])


def test_live_queues_classify_thresholds_without_sample_limits(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    for level in (-36,-35,-34,-37):
        record_id=store.save(pcm(),16000,{'status':'low_rms'})
        with store.connect() as db:
            db.execute("UPDATE recordings SET data=json_set(data,'$.signal.rms_dbfs',?) WHERE id=?",(level,record_id))
    d=[store.save(pcm(),16000,{'status':'vad_short','speech_ms':300+i}) for i in range(22)]
    store.save(pcm(),16000,{'status':'vad_short','speech_ms':299})
    store.save(pcm(),16000,{'status':'notification'})
    ready=store.save(pcm(),16000,{'status':'empty_text'})
    progress={g['key']:g for g in store.queue_progress()}
    assert {k:g['total'] for k,g in progress.items()}=={'A':1,'B':2,'C':1,'D':22}
    assert store.listing(queue='A')['items'][0]['id']==ready
    assert store.listing(queue='all')['total']==26
    assert [r['id'] for r in store.listing(queue='D',limit=2)['items']]==d[:2]
    store.review(d[0],'','noise',0)
    pending=store.listing(queue='D',review='pending')
    assert pending['total']==21 and pending['items'][0]['id']==d[1]
    assert store.get(d[0])['label']=='noise'



def test_room_label_and_multiple_messages_flag(tmp_path):
    import sqlite3
    from services.recordings import RecordingStore
    root = tmp_path / "archive"
    root.mkdir()
    # Старий архів без нового стовпця: міграція лише додає його.
    with sqlite3.connect(root / "index.sqlite3") as db:
        db.execute("""CREATE TABLE recordings (id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
            bytes INTEGER NOT NULL, data TEXT NOT NULL, reference TEXT, label TEXT, revision INTEGER NOT NULL DEFAULT 0,
            reviewed_at REAL, reference_recipient TEXT, reference_sender TEXT)""")
    store = RecordingStore(root)
    speech = store.save(pcm() * 3, 16000, {"status": "ready", "final_text": "вишня"})
    room = store.save(pcm(), 16000, {"status": "ready", "final_text": "дякую"})
    assert store.review(speech, "вишня 210 прийом клен 300", "speech", 0, multiple_messages=True)["multiple_messages"] == 1
    saved = store.review(room, "", "room", 0, reference_recipient="x", multiple_messages=True)
    assert saved["reference_recipient"] == "" and saved["multiple_messages"] is None
    summary = store.summary()
    assert (summary["room"], summary["room_false_text"], summary["multiple_messages"], summary["speech_reviewed"]) == (1, 1, 1, 1)
    assert summary["false_text"] == 0, "кімнатний шум не входить у хибний текст ефіру"
    store.review(speech, "вишня 210", "speech", 1)
    assert store.get(speech)["multiple_messages"] == 1, "без поля відмітка не стирається"
