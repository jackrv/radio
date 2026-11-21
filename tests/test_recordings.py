import io
from pathlib import Path
import json
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
    assert store.listing(view='reviewed')['total'] == 1
    assert store.listing(view='radio')['total'] == 0
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
    from core.recording_trace import _trace
    from tests.test_event_processor import make_processor
    from core.callsigns import Callsigns
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


def test_views_partition_pending_archive_and_preserve_reviewed_records(tmp_path):
    store = RecordingStore(tmp_path/'archive')
    filtered = store.save(pcm(), 16000, {'status':'low_rms'})
    speech = store.save(pcm()*3, 16000, {'status':'ready'})
    radio = store.save(pcm(), 16000, {'status':'ready', 'fix_id':'db:7'})
    assert store.listing(view='filtered', limit=1)['total'] == 1
    assert store.listing(view='speech')['items'][0]['id'] == speech
    assert store.listing(view='radio')['items'][0]['id'] == radio
    store.review(filtered, '', 'noise', 0)
    assert store.listing(view='filtered')['total'] == 0
    assert store.listing(view='reviewed')['items'][0]['id'] == filtered
    assert store.listing()['total'] == 3



def test_recording_view_api_validation(setup, tmp_path):
    _, db = setup
    store = RecordingStore(tmp_path/'archive')
    store.save(pcm(), 16000, {'status':'ready'})
    normal = store.save(pcm()*3, 16000, {'status':'ready'})
    app = create_dashboard(db, 'x'*43, recordings_dir=store.root)
    with TestClient(app, base_url='http://127.0.0.1:8766', client=('127.0.0.1',1234)) as client:
        client.post('/session', json={'token':'x'*43}, headers={'Origin':'http://127.0.0.1:8766'})
        page = client.get('/api/recordings?view=speech').json()
        assert page['total'] == 1 and page['items'][0]['id'] == normal
        assert client.get('/api/recordings?view=invalid').status_code == 422
        assert client.get('/api/recordings').json()['total'] == 2
        assert client.get('/api/recordings/queue').status_code == 404
        for parameter in ('group', 'queue', 'review'):
            assert client.get('/api/recordings', params={parameter:'all'}).status_code == 422



def test_filtered_view_is_live_and_has_no_sample_limit(tmp_path):
    store = RecordingStore(tmp_path/'archive')
    ids = [store.save(pcm(), 16000, {'status':'vad_short'}) for _ in range(22)]
    assert store.listing(view='filtered')['total'] == 22
    store.review(ids[0], '', 'noise', 0)
    assert store.listing(view='filtered')['total'] == 21
    new = store.save(pcm(), 16000, {'status':'notification'})
    assert store.listing(view='filtered', limit=2)['items'][0]['id'] == new
    assert store.get(ids[0])['label'] == 'noise'




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


def test_when_space_runs_out_filtered_noise_goes_first_and_radio_last(tmp_path):
    """Нестача місця: спершу відсіяне програмою, потім інше мовлення, ефір — останнім."""
    from unittest.mock import patch
    store = RecordingStore(tmp_path / 'archive')
    audio = b'\x00\x01' * (16000 * 3)          # 3 с: «інше мовлення» — довше за 2 с
    radio_old = store.save(audio, 16000, {'status': 'ready', 'fix_id': 'db:1'})
    speech_old = store.save(audio, 16000, {'status': 'ready'})
    quiet_new = store.save(audio, 16000, {'status': 'low_rms'})
    size = store.get(quiet_new)['bytes']
    store.max_bytes = 2 * size          # вміщується лише два записи
    store.prune()
    kept = {r['id'] for r in store.listing(view='all', limit=10)['items']}
    assert kept == {radio_old, speech_old}, "найновіша тиша поступається старішому ефіру й мовленню"
    store.max_bytes = size
    store.prune()
    assert {r['id'] for r in store.listing(view='all', limit=10)['items']} == {radio_old}
    with patch('services.recordings.time.time', return_value=10**10):
        store.prune()
    assert store.listing(view='all', limit=10)['items'] == [], "строк за віком діє для всіх нерозмічених"


def test_quality_by_day_counts_phrases_that_needed_no_edits_and_missed_speech(tmp_path):
    store = RecordingStore(tmp_path / 'archive')
    for heard, final, reference in (('клен 15 прийом', 'Клен 15, прийом', 'Клен 15, прийом'),
                                    ('клин 15', 'клин 15', 'Клен 15, прийом'),
                                    ('', '', 'Клен 15, прийом')):
        rid = store.save(pcm(), 16000, {'status': 'ready', 'raw_text': heard, 'final_text': final})
        store.review(rid, reference, 'speech', 0)
    day, = store.quality_by_day()
    assert day['records'] == 3 and day['no_edits'] == 1 and day['missed'] == 1
    assert round(day['no_edits_share'], 2) == 0.33


def test_training_set_readiness_and_local_export(tmp_path):
    import json as _json
    from services.training_set import export, readiness
    store = RecordingStore(tmp_path / 'archive')
    for i, (label, reference) in enumerate((('speech', 'Клен 15, прийом / Дуб 4, прийнято'), ('speech', 'Клен 15, прийнято'),
                                            ('noise', ''), ('unclear', 'щось'))):
        rid = store.save(pcm(), 16000, {'status': 'ready'})
        store.review(rid, reference, label, 0)
    ready = readiness(store)
    assert ready['records'] == 2 and ready['seconds'] > 0 and ready['targets'][0]['seconds'] == 7200
    result = export(store, tmp_path / 'training')
    rows = [_json.loads(l) for l in (Path(result['path']) / 'metadata.jsonl').read_text().splitlines()]
    assert result['records'] == 2 and {r['text'] for r in rows} == {'Клен 15, прийом Дуб 4, прийнято', 'Клен 15, прийнято'}
    assert all((Path(result['path']) / r['audio']).is_file() for r in rows)


def test_listing_and_view_counts_respect_a_closed_period(tmp_path):
    import time as _time
    store = RecordingStore(tmp_path / 'archive')
    now = _time.time()
    ids = {}
    for name, age in (('today', 0), ('yesterday', 86400 * 1.2), ('older', 86400 * 3)):
        ids[name] = store.save(pcm(), 16000, {'status': 'ready'})
        with store.connect() as db:
            db.execute("UPDATE recordings SET created=? WHERE id=?", (now - age, ids[name]))
    since, until = now - 86400 * 2, now - 86400
    assert [i['id'] for i in store.listing(since=since, until=until)['items']] == [ids['yesterday']]
    assert store.view_counts(since, until)['all'] == 1 and store.view_counts()['all'] == 3


def test_summary_counts_phrases_without_edits_and_todays_reviews(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    exact=store.save(pcm(),16000,{'status':'ready','raw_text':'клен сорок','final_text':'Клен, сорок.'})
    wrong=store.save(pcm(),16000,{'status':'ready','raw_text':'клен','final_text':'клен'})
    store.review(exact,'клен сорок','speech',0)
    store.review(wrong,'клен сорок','speech',0)
    summary=store.summary()
    assert summary['no_edits']==1 and summary['quality']['final_text']['reviewed_speech']==2
    assert summary['reviewed_today']==2
    assert summary['speech_today']==2 and summary['no_edits_today']==1


def test_summary_skips_unused_number_checks_and_reads_day_boundary_once(tmp_path, monkeypatch):
    from services import recordings
    store = RecordingStore(tmp_path / 'archive')
    record_id = store.save(pcm(), 16000, {'status': 'ready', 'raw_text': 'Клен 15', 'final_text': 'Клен 15'})
    store.review(record_id, 'Клен 15', 'speech', 0)
    # Детальна картка зберігає перевірку чисел; зведення її не використовує.
    assert store.get(record_id)['quality']['final_text']['number_match'] is True
    expected = store.summary()
    original = recordings._day_start
    calls = []
    def day_start():
        calls.append(True)
        return original()
    def unused(*args):
        raise AssertionError('summary must not normalize number groups')
    monkeypatch.setattr(recordings, '_day_start', day_start)
    monkeypatch.setattr(recordings, 'scores', unused)
    assert store.summary() == expected
    assert len(calls) == 1


def test_audio_supports_byte_ranges_for_seeking():
    from api.panel.recordings import ranged_audio
    data = bytes(range(100))
    full = ranged_audio(data, None)
    assert full.status_code == 200 and full.body == data and full.headers['accept-ranges'] == 'bytes'
    part = ranged_audio(data, 'bytes=10-19')
    assert part.status_code == 206 and part.body == data[10:20] and part.headers['content-range'] == 'bytes 10-19/100'
    assert ranged_audio(data, 'bytes=90-').body == data[90:]
    assert ranged_audio(data, 'bytes=-5').body == data[95:]
    assert ranged_audio(data, 'bytes=200-').status_code == 416
    assert ranged_audio(data, 'items=1-2').status_code == 200


def test_reviewed_view_orders_and_filters_by_review_time(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    old=store.save(pcm(),16000,{'status':'ready','raw_text':'клен','final_text':'клен'})
    new=store.save(pcm(),16000,{'status':'ready','raw_text':'дуб','final_text':'дуб'})
    store.review(new,'','noise',0)
    store.review(old,'','room',0)   # older recording, reviewed last
    assert [i['id'] for i in store.listing(view='reviewed')['items']]==[old,new]
    assert [i['id'] for i in store.listing(view='all')['items']]==[new,old]
    with store.connect() as db:
        db.execute("UPDATE recordings SET reviewed_at=? WHERE id=?",(1000.0,new))
    assert [i['id'] for i in store.listing(view='reviewed',since=2000)['items']]==[old]
    counts=store.view_counts(since=2000)
    assert counts['reviewed']==1 and counts['all']==2


def test_check_views_find_program_errors_and_incomplete_labels(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    false_text=store.save(pcm(),16000,{'status':'ready','raw_text':'клен','final_text':'клен'})
    missed=store.save(pcm(),16000,{'status':'empty_text','raw_text':'','final_text':''})
    no_roles=store.save(pcm(),16000,{'status':'ready','raw_text':'дуб 7','final_text':'дуб 7'})
    with_roles=store.save(pcm(),16000,{'status':'ready','raw_text':'дуб 7','final_text':'дуб 7'})
    unclear=store.save(pcm(),16000,{'status':'ready','raw_text':'щось','final_text':'щось'})
    store.review(false_text,'','noise',0)
    store.review(missed,'клен сорок','speech',0,reference_recipient='клен')
    store.review(no_roles,'дуб 7','speech',0)
    store.review(with_roles,'дуб 7','speech',0,reference_recipient='',reference_sender='дуб 7')
    store.review(unclear,'щось','unclear',0)
    ids=lambda view:[i['id'] for i in store.listing(view=view)['items']]
    assert ids('false_text')==[false_text]
    assert ids('missed')==[missed]
    assert ids('no_roles')==[no_roles]
    assert ids('unclear')==[unclear]
    assert store.view_counts()['no_roles']==1



def test_restore_returns_a_quick_label_to_the_previous_state(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    record_id=store.save(pcm(),16000,{'status':'ready','raw_text':'клен','final_text':'клен'})
    before={k:store.get(record_id)[k] for k in ('reference','label','reference_recipient','reference_sender','multiple_messages','reviewed_at','reference_source')}
    labelled=store.review(record_id,'','noise',0)
    restored=store.restore(record_id,labelled['revision'],before)
    assert restored['reference'] is None and restored['label'] is None and restored['reviewed_at'] is None
    assert restored['revision']==2
    with pytest.raises(ValueError):
        store.restore(record_id,labelled['revision'],before)   # stale revision is refused
    store.review(record_id,'клен сорок','speech',2,reference_recipient='клен')
    previous=store.get(record_id)
    state={k:previous[k] for k in before}
    noise=store.review(record_id,'','room',3)
    back=store.restore(record_id,noise['revision'],state)
    assert back['reference']=='клен сорок' and back['label']=='speech' and back['reference_recipient']=='клен'
    assert back['reviewed_at']==previous['reviewed_at']


def test_doubtful_order_puts_least_confident_first_and_threshold_needs_enough_labels(tmp_path):
    store=RecordingStore(tmp_path/'archive')
    shaky=store.save(pcm(),16000,{'status':'ready','raw_text':'дуб','final_text':'дуб','whisper_segments':[{'avg_logprob':-1.3}]})
    sure=store.save(pcm(),16000,{'status':'ready','raw_text':'клен','final_text':'клен','whisper_segments':[{'avg_logprob':-0.2}]})
    empty=store.save(pcm(),16000,{'status':'empty_text','raw_text':'','final_text':''})
    assert [i['id'] for i in store.listing(order='doubtful')['items']]==[empty,shaky,sure], "без тексту й найменш упевнені — першими"
    assert [i['id'] for i in store.listing()['items']]==[empty,sure,shaky], "типово — нові першими"
    assert store.confidence_threshold() is None, "менше 20 розмічених фраз — межі немає"
    for n in range(20):
        r=store.save(pcm(),16000,{'status':'ready','raw_text':'х','final_text':'х','whisper_segments':[{'avg_logprob':-n/10}]})
        store.review(r,'х','speech',0)
    assert store.confidence_threshold()==-1.4
