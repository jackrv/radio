"""Синтетичний стрес SQLite: пауза/hold, запис, розмітка, читання; лише тимчасові БД."""
import sys,tempfile,multiprocessing as mp,json,time,traceback,sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.sqlite_connection import get_db
from services.receiver_control import set_pause_minutes,set_listening_hold,reception_request
from services.recordings import RecordingStore

def worker(role,root,count):
    root=Path(root);path=root/'events.db';store=RecordingStore(root/'archive')
    for i in range(count):
        if role=='hold':
            set_listening_hold(path,10 if i%2 else 0,now=1100)
            assert reception_request(path,now=1100)=='pause'
        elif role=='receiver':
            with get_db(path) as db:db.execute('INSERT INTO synthetic_events VALUES(?)',(i,))
            store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12, прийом'})
        elif role=='review':
            item=store.listing(limit=1)['items'][0]
            store.review(item['id'],'Клен 12, прийом','speech',item['revision'])
            with get_db(path) as db:db.execute('INSERT INTO synthetic_reviews VALUES(?)',(i,))
        else:
            with get_db(path) as db:db.execute('SELECT COUNT(*) FROM synthetic_events').fetchone()
            store.summary();store.listing(limit=5)
    print(json.dumps({'role':role,'completed':count}),flush=True)

if __name__=='__main__':
    mp.set_start_method('spawn')
    with tempfile.TemporaryDirectory(prefix='radio-sqlite-concurrency-') as tmp:
        root=Path(tmp);path=root/'events.db';count=200
        with get_db(path) as db:
            db.execute('CREATE TABLE synthetic_events(i INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE synthetic_reviews(i INTEGER PRIMARY KEY)')
        set_pause_minutes(path,60,now=1000)
        store=RecordingStore(root/'archive');store.save(b'\0\0'*1600,16000,{'status':'ready','raw_text':'Клен 12, прийом'})
        started=time.monotonic()
        ps=[mp.Process(target=worker,args=(role,tmp,count)) for role in ['hold','receiver','review','read']]
        for p in ps:p.start()
        for p in ps:p.join(60)
        if any(p.is_alive() for p in ps):
            for p in ps:
                if p.is_alive():p.terminate();p.join()
            raise RuntimeError('stress timeout')
        assert all(p.exitcode==0 for p in ps),[p.exitcode for p in ps]
        with get_db(path) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
            counts=[db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['synthetic_events','synthetic_reviews']]
        with store.connect() as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            archive_count=db.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        assert counts==[count,count] and archive_count==count+1
        print(json.dumps({'processes':len(ps),'iterations_each':count,'committed_counts':counts,'recordings':archive_count,'integrity':'ok','elapsed_s':round(time.monotonic()-started,3)}))
