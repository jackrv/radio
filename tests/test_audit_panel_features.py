import sqlite3

from tests.test_local_dashboard import local, no_background_spawn, finish_job, ORIGIN
from tests.test_miniapp_management import setup
from shared.recordings import RecordingStore


def test_telegram_sync_resolution_requires_local_origin(local):
    client,path,tmp=local
    assert client.post('/api/telegram-sync/retry').status_code==403
    assert client.post('/api/telegram-sync/retry',headers={'Origin':ORIGIN}).json()=={'action':'retry','count':0}
    assert client.post('/api/telegram-sync/skip',headers={'Origin':ORIGIN}).json()=={'action':'skip','count':0}
    assert client.post('/api/telegram-sync/invalid',headers={'Origin':ORIGIN}).status_code==422


def test_selection_api_snapshot_and_archive_transfer(local):
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    a=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    result=client.post('/api/mentions',headers={'Origin':ORIGIN},
        json={'terms':'клен 12','since':0,'until':4e9,'order':'old'})
    assert result.status_code==200,result.text
    selection=result.json()['selection']
    assert result.json()['total']==1
    # Пізні нові записи не змінюють уже створену вибірку.
    store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    page=client.get('/api/mentions',params={'selection':selection}).json()
    assert page['total']==1
    archive=client.get('/api/recordings',params={'selection':selection,'order':'old'}).json()
    assert archive['total']==1 and archive['items'][0]['id']==a
    assert client.post('/api/mentions',json={'terms':'x','since':0,'until':1}).status_code==403


def test_selection_api_changes_sort_without_new_snapshot_or_new_records(local):
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    first=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    store.review(first,'Клен 12','speech',0)
    second=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    selected=client.post('/api/mentions',headers={'Origin':ORIGIN},json={'terms':'клен','since':0,'until':4e9}).json()
    store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    token=selected['selection']
    page=client.get('/api/mentions',params={'selection':token,'order':'unreviewed'}).json()
    assert page['total']==2 and page['items'][0]['id']==second
    assert page['selection']==token and page['order']=='unreviewed'
    archive=client.get('/api/recordings',params={'selection':token,'order':'unreviewed','view':'all'}).json()
    assert archive['items'][0]['id']==second and archive['total']==2
    assert client.get('/api/mentions',params={'selection':token,'order':'invalid'}).status_code==422


def test_editor_highlights_are_optional_and_read_only(local):
    client, path, tmp = local
    request = {'text': 'Береза 56 / клєн 13', 'highlights': True}
    response = client.post('/api/text-check', headers={'Origin': ORIGIN}, json=request)
    assert response.status_code == 200
    assert isinstance(response.json()['highlights'], list)
    response = client.post('/api/text-check', headers={'Origin': ORIGIN}, json={'text':'Береза 56'})
    assert 'highlights' not in response.json()
    assert client.post('/api/text-check', json=request).status_code == 403


def test_archive_inventory_is_background_job(local):
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'synthetic'})
    assert client.post('/api/archive-backups',headers={'Origin':ORIGIN}).status_code in (404,405)
    report=finish_job(client,path,client.post('/api/archive-inventory',headers={'Origin':ORIGIN}))
    assert report['missing']==[] and report['orphans']==[]


def test_auto_delete_is_off_until_configured_and_never_deletes_archive(tmp_path):
    from shared.telegram_sync import remember_delivery
    from shared.telegram_recall import expire_deliveries,state
    from shared.runtime_settings import read_runtime_settings,write_runtime_settings
    path=tmp_path/'events.db'
    remember_delivery(path,'db:1',{'chat':{'id':11},'message_id':5},'text')
    with sqlite3.connect(path) as db:db.execute('UPDATE telegram_deliveries SET sent_at=100')
    assert expire_deliveries(path,now=4000)==0
    config=read_runtime_settings(path)
    write_runtime_settings(path,{'TELEGRAM_AUTO_DELETE_HOURS':1},config['revision'])
    assert expire_deliveries(path,now=4000)==1
    status=state(path,['db:1'])['db:1']
    assert status['pending']==1 and not status['hidden']
    assert expire_deliveries(path,now=4001)==0


def test_operator_decision_is_retained_and_can_be_restored_individually(tmp_path):
    from shared.suggestions import ensure_table,dismiss,restore,decisions
    from shared.sqlite_connection import get_db
    path=tmp_path/'events.db'
    with get_db(path) as db:
        ensure_table(db)
        db.execute("INSERT INTO suggestion_state(key,kind,title,first_seen) VALUES ('synthetic','noise','Synthetic example',1)")
    assert dismiss(path,'noise',now=10)==1
    with get_db(path) as db:db.execute('DELETE FROM suggestion_state')
    result=decisions(path)
    assert result['total']==1 and result['items'][0]['title']=='Synthetic example'
    assert restore(path,keys=['synthetic'])==1
    assert decisions(path)['total']==0


def test_activity_records_allowed_details_without_arbitrary_payload():
    from server.user_activity import panel_details
    result=panel_details({'token':'DO_NOT_STORE','entries':[{'raw_line':'DO_NOT_STORE','enabled':True}],
                          'values':{'VAD_LEVEL':2,'token':'DO_NOT_STORE'}})
    assert result=={'rows':1,'enabled_rows':1,'values':{'VAD_LEVEL':2}}


def test_archive_learning_counts_sources_not_repeated_saves(local):
    from shared.management.learning import learning_queue
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    ids=[store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'позицю'}) for _ in range(2)]
    for record_id in ids:
        response=client.put(f'/api/recordings/{record_id}/review',headers={'Origin':ORIGIN},
            json={'reference':'позицію','label':'speech','expected_revision':0})
        assert response.status_code==200,response.text
        assert response.json()['learning_sync']['status']=='synced'
    response=client.put(f'/api/recordings/{ids[0]}/review',headers={'Origin':ORIGIN},
        json={'reference':'позицію','label':'speech','expected_revision':1})
    assert response.status_code==200
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT confirmations FROM word_corrections WHERE original='позицю' AND corrected='позицію'").fetchone()[0]==2
    rule=next(r for r in learning_queue(path)['items'] if r['original']=='позицю')
    assert rule['state']=='needs_approval' and not rule['applies']
    # Класифікація як шум відкликає голос, а не залишає старе навчання.
    response=client.put(f'/api/recordings/{ids[0]}/review',headers={'Origin':ORIGIN},
        json={'reference':'','label':'noise','expected_revision':2})
    assert response.status_code==200
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT confirmations FROM word_corrections WHERE original='позицю' AND corrected='позицію'").fetchone()[0]==1


def test_saved_archive_learning_is_idempotent_and_creates_backup(local):
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    rid=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'позицю'})
    store.review(rid,'позицію','speech',0)
    for _ in range(2):
        result=finish_job(client,path,client.post('/api/learning/archive',headers={'Origin':ORIGIN}))
        assert result['synced']==1 and result['failed']==0
        assert (tmp/'.local-backups'/result['backup']).exists()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT confirmations FROM word_corrections WHERE original='позицю' AND corrected='позицію'").fetchone()[0]==1


def test_archive_and_mem_editor_share_one_training_source(local):
    from shared.telegram_phrase_store import register_phrase
    from shared.word_store import save_word_feedback
    client,path,tmp=local
    token='a'*32;source='mem:'+token
    register_phrase(path,token,'позицю')
    save_word_feedback(path,source,'позицю','позицію')
    store=RecordingStore(tmp/'recordings')
    rid=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'позицю','fix_id':source})
    response=client.put(f'/api/recordings/{rid}/review',headers={'Origin':ORIGIN},
        json={'reference':'позицію','label':'speech','expected_revision':0})
    assert response.status_code==200
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT confirmations FROM word_corrections WHERE original='позицю' AND corrected='позицію'").fetchone()[0]==1
        assert db.execute('SELECT DISTINCT source_id FROM word_correction_votes').fetchall()==[(source,)]


def test_archive_selection_api_excludes_mentions_removed_by_reference(local):
    client,path,tmp=local
    store=RecordingStore(tmp/'recordings')
    key=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12'})
    selection=client.post('/api/mentions',headers={'Origin':ORIGIN},json={'terms':'клен','since':0,'until':4e9}).json()['selection']
    store.review(key,'Дуб 34','speech',0)
    assert client.get('/api/mentions',params={'selection':selection}).json()['total']==0
    assert client.get('/api/recordings',params={'selection':selection,'view':'all'}).json()['total']==0
