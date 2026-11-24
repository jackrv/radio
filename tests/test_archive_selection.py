import json
import os
import queue
import sqlite3
import threading
from pathlib import Path

import pytest

from shared.recordings import RecordingStore
from shared.archive_inventory import inventory
from shared.mention_selection import find, save_selection, load_selection
from shared.paths import place


def corpus(tmp_path):
    store=RecordingStore(tmp_path/'recordings')
    a=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12, прийом'})
    b=store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Кленок 12'})
    with store.connect() as db:
        db.execute('UPDATE recordings SET created=100 WHERE id=?',(a,))
        db.execute('UPDATE recordings SET created=200 WHERE id=?',(b,))
    return store,a,b


def test_selection_order_override_sorts_all_pages_and_archive_without_changing_membership(tmp_path):
    from shared.mention_selection import page_selection
    store=RecordingStore(tmp_path/'records')
    with store.connect() as db:
        for i in range(205):
            db.execute('INSERT INTO recordings(id,created,status,bytes,data,reference,label) VALUES (?,?,?,?,?,?,?)',
                       (f'{i+1:032x}',1000+i,'ready',0,json.dumps({'raw_text':'Клен 12'}),
                        'Клен 12' if i<105 else None,'speech' if i<105 else None))
    found=find(store.root,'клен',0,4e9)
    saved=load_selection(tmp_path/'events.db',save_selection(tmp_path/'events.db',found))
    before=list(saved['ids'])
    page=page_selection(store.root,saved,0,100,order='unreviewed')
    assert page['total']==205 and all(not row['reviewed'] for row in page['items'])
    assert page['items'][0]['id']==f'{106:032x}'
    assert page_selection(store.root,saved,100,100,order='unreviewed')['items'][0]['id']==f'{1:032x}'
    assert page_selection(store.root,saved,0,1,order='new')['items'][0]['id']==f'{205:032x}'
    archive=store.listing(order='unreviewed',selected_ids=before,selection_terms=saved['terms'])
    assert archive['items'][0]['id']==f'{106:032x}'
    assert saved['ids']==before and saved['order']=='old'
    assert find(store.root,'клен',0,4e9,order='unreviewed')['ids'][0]==f'{106:032x}'
    with pytest.raises(ValueError):page_selection(store.root,saved,0,100,order='invalid')


def test_inventory_grace_and_no_automatic_deletion(tmp_path):
    store,a,_=corpus(tmp_path)
    orphan=store.root/('f'*32+'.wav');orphan.write_bytes(b'synthetic')
    os.utime(orphan,(1,1))
    recent=store.root/('e'*32+'.wav');recent.write_bytes(b'recent')
    (store.root/(a+'.wav')).unlink()
    result=inventory(store.root)
    assert result['orphans']==['f'*32] and result['missing']==[a]
    assert orphan.exists() and recent.exists()


def test_mentions_boundaries_time_and_selection_membership(tmp_path):
    store,a,b=corpus(tmp_path)
    result=find(store.root,'клен 12',0,300)
    assert [i['id'] for i in result['items']]==[a]
    assert find(store.root,'клен',101,300)['total']==0
    both=find(store.root,'клен, кленок',0,300)
    assert both['ids']==[a,b]
    token=save_selection(tmp_path/'events.db',both)
    assert load_selection(tmp_path/'events.db',token)['ids']==[a,b]
    assert [r['id'] for r in store.listing(order='old', selected_ids=[a,b])['items']]==[a,b]
    assert store.listing(selected_ids=[])['total']==0
    with pytest.raises(ValueError):load_selection(tmp_path/'events.db','../events.db')


def test_expired_selections_are_pruned_without_touching_other_files(tmp_path):
    store,_,_=corpus(tmp_path)
    result=find(store.root,'клен',0,300)
    token=save_selection(tmp_path/'events.db',result)
    path=place(tmp_path, ".local-selections")/(token+'.json');os.utime(path,(1,1))
    other=path.parent/'notes.json';other.write_text('synthetic')
    save_selection(tmp_path/'events.db',result)
    assert not path.exists() and other.exists()


def test_mentions_exclude_non_speech_and_use_chronology_on_every_page(tmp_path):
    from shared.mention_selection import page_selection
    store, a, b = corpus(tmp_path)
    store.review(a, 'Клен 12, прийом', 'speech', 0)
    with store.connect() as db:
        db.execute("UPDATE recordings SET data=? WHERE id=?", (json.dumps({'raw_text':'Клен 12'}), b))
    found = find(store.root, 'клен', 0, 300)
    assert found['ids'] == [a, b]
    token = save_selection(tmp_path/'events.db', found)
    saved = load_selection(tmp_path/'events.db', token)
    assert page_selection(store.root, saved, 0, 1)['items'][0]['id'] == a
    assert store.listing(order='old', selected_ids=[a,b])['items'][0]['id'] == a
    for label in ('noise','notification','room'):
        current = store.get(b)
        store.review(b, '', label, current['revision'])
        assert find(store.root, 'клен', 0, 300)['ids'] == [a]
        assert page_selection(store.root, saved, 0, 100)['total'] == 1
        assert store.listing(selected_ids=[a,b])['total'] == 1
        # Зберігаємо помилковий ASR для негативного контролю, не як еталон.
        assert store.get(b)['reference'] == ''
        assert store.get(b)['data']['raw_text'] == 'Клен 12'


def test_model_slot_blocks_competing_receivers_and_labs(tmp_path):
    from shared.instance_lock import acquire_instance_lock
    from shared.model_exclusion import lab_model_slot,receiver_model_available
    path=tmp_path/'events.db'
    with sqlite3.connect(path) as db:db.execute('create table synthetic(id int)')
    receiver=acquire_instance_lock(place(tmp_path, ".radio345.lock"))
    try:
        with pytest.raises(RuntimeError):
            with lab_model_slot(path): pytest.fail('модель не має завантажитися')
    finally:os.close(receiver)
    with lab_model_slot(path):
        assert not receiver_model_available(tmp_path)
        with pytest.raises(RuntimeError):
            with lab_model_slot(path):pass
    assert receiver_model_available(tmp_path)


def test_abandoned_inflight_asr_has_no_late_event_or_archive_submission():
    from tests.test_transcriber import make_transcriber,make_pcm
    from unittest.mock import Mock
    worker,recognizer,_,processor=make_transcriber()
    entered=threading.Event();release=threading.Event()
    worker.recorder=Mock()
    def recognize(_):
        entered.set();assert release.wait(2);return {'text':'synthetic'}
    recognizer.transcribe.side_effect=recognize
    thread=threading.Thread(target=lambda:worker.process_segment(make_pcm()))
    thread.start();assert entered.wait(2)
    worker.abandon_event.set();release.set();thread.join(2)
    assert not thread.is_alive()
    assert not processor.process.called and not worker.recorder.submit.called


def test_mention_reference_is_authoritative_and_snapshot_rechecks_current_text(tmp_path):
    from shared.mention_selection import page_selection
    store,a,b=corpus(tmp_path)
    with store.connect() as db:
        db.execute('UPDATE recordings SET data=? WHERE id=?',(json.dumps({'raw_text':'Клен 12','final_text':'Клен 12'}),b))
    original=find(store.root,'клен',0,300)
    saved=load_selection(tmp_path/'events.db',save_selection(tmp_path/'events.db',original))
    store.review(a,'Дуб 34, прийом','speech',0)
    assert find(store.root,'клен',0,300)['ids']==[b]
    page=page_selection(store.root,saved,0,1)
    assert page['total']==1 and page['items'][0]['id']==b
    archive=store.listing(order='old',selected_ids=saved['ids'],selection_terms=saved['terms'])
    assert archive['total']==1 and archive['items'][0]['id']==b
    assert find(store.root,'дуб',0,300)['ids']==[a]
    store.review(b,'','unclear',0)
    assert find(store.root,'клен',0,300)['total']==0
    assert page_selection(store.root,saved,0,1)['total']==0


def test_unreviewed_search_uses_final_text_and_never_falls_back_from_empty_final(tmp_path):
    store,a,b=corpus(tmp_path)
    with store.connect() as db:
        db.execute('UPDATE recordings SET data=? WHERE id=?',(json.dumps({'raw_text':'Клен 12','final_text':'Дуб 34'}),a))
        db.execute('UPDATE recordings SET data=? WHERE id=?',(json.dumps({'raw_text':'Клен 12','final_text':''}),b))
    assert find(store.root,'клен',0,300)['total']==0
    assert find(store.root,'дуб',0,300)['ids']==[a]


def test_reviewed_and_unreviewed_share_strict_time_order(tmp_path):
    store,a,b=corpus(tmp_path)
    store.review(a,'Клен 12','speech',0)
    with store.connect() as db:db.execute('UPDATE recordings SET data=? WHERE id=?',(json.dumps({'raw_text':'Клен 12'}),b))
    assert find(store.root,'клен',0,300,order='old')['ids']==[a,b]
    assert find(store.root,'клен',0,300,order='new')['ids']==[b,a]


def test_mentions_skip_unreviewed_records_filtered_by_program(tmp_path):
    from shared.mention_selection import page_selection
    import time
    now = time.time() - 60
    store = RecordingStore(tmp_path/'recordings')
    a = store.save(b'\0\0'*1600, 16000, {'status': 'ready', 'raw_text': 'Клен 12', 'captured_at': now})
    c = store.save(b'\0\0'*1600, 16000, {'status': 'blacklist', 'raw_text': 'Клен 7', 'captured_at': now + 1})
    d = store.save(b'\0\0'*1600, 16000, {'status': 'low_confidence', 'raw_text': 'Клен 8', 'captured_at': now + 2})
    assert find(store.root, 'клен', 0, 4e9)['ids'] == [a]
    # Розмічене оператором як мовлення лишається у вибірці за еталоном.
    store.review(d, 'Клен 8, прийом', 'speech', store.get(d)['revision'])
    found = find(store.root, 'клен', 0, 4e9)
    assert found['ids'] == [a, d]
    saved = load_selection(tmp_path/'events.db', save_selection(tmp_path/'events.db', found))
    assert [item['id'] for item in page_selection(store.root, saved, 0, 100)['items']] == [a, d]
    assert {item['id'] for item in store.listing(limit=100, selected_ids=[a, c, d])['items']} == {a, d}
