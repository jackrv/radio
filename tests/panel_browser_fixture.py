"""Тільки синтетична БД/аудіо для tools/check_panel_ui.cjs; без живих служб."""
import sys,json,socket,math,struct
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import uvicorn
import sqlite3
from services.dictionary_store import init_db
from services.event_store import init_db as init_events
from services.user_store import init_users_db,bootstrap_admin
from services.recordings import RecordingStore
from api.local_dashboard import create_dashboard
root=Path(sys.argv[1]);db=root/'events.db'
init_db(db);init_events(db);init_users_db(db);bootstrap_admin(db,1001)
with sqlite3.connect(db) as connection:
 connection.executemany("INSERT INTO dictionary_entries(dictionary_type,line_number,raw_line) VALUES('callsigns',?,?)",[(1,'Клен: 12'),(2,'Дуб: 34')])
from services.telegram_sync import remember_delivery,queue_correction
from services.sqlite_connection import get_db
remember_delivery(db,'db:999',{'chat':{'id':1001},'message_id':1},'voice')
with get_db(db) as connection:
 queue_correction(connection,'db:999')
 connection.execute('UPDATE telegram_deliveries SET attempts=8')
from services.word_store import save_word_feedback
for token in ('1','2'):
 save_word_feedback(db,'archive:'+token*32,'дорга','дорога')
store=RecordingStore(root/'recordings')
for text in ['Клен 12, прийом','Клен 12, я Дуб 34','Без збігу']:
 store.save(b''.join(struct.pack('<h',int(8000*math.sin(i*.1))) if .2 <= i/16000 < 1.4 or 2 <= i/16000 < 3.7 else b'\0\0' for i in range(64000)),16000,{'status':'ready','raw_text':text,'final_text':text,'signal':{'duration_s':4},'predicted_recipient':'Клен 12' if 'Клен' in text else ''})
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
app=create_dashboard(db,'x'*43,port=port,samples_dir=root/'samples',diagnostics_path=root/'diagnostics.json',
 recordings_dir=store.root,launcher_root=root,experiments_dir=root/'experiments',backup_dir=root/'backups')
print(json.dumps({'port':port}),flush=True)
uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[sock])
