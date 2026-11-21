"""Голоси перевірених записів архіву: одна фраза — одне стійке джерело."""
import json
import logging
import re
import time
from contextlib import closing

from services.sqlite_connection import get_db,read_only
from services.word_store import init_word_db,save_word_feedback_report
from services.user_store import _require_permission


def sync_archive_learning(db_path, store, record_id, actor):
    """Розмітка вже збережена в окремому індексі. Збій голосування явно повертається;
    повтор цього методу або збереження фрази відновлює синхронізацію без нових голосів.
    """
    try:
        init_word_db(db_path)
        with get_db(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            _require_permission(db,actor,'dictionaries.write')
            # Читаємо свіжу версію після write-lock голосів: пізній обробник
            # не поверне голоси до попереднього еталона.
            with closing(read_only(store.root/'index.sqlite3')) as archive:
                row=archive.execute('SELECT reference,label,data,revision FROM recordings WHERE id=?',(record_id,)).fetchone()
            if row is None: return {'status':'missing'}
            reference,label,data,revision=row;data=json.loads(data)
            source=data.get('fix_id') or ''
            if not isinstance(source,str) or not re.fullmatch(r'(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{8,32})',source): source='archive:'+record_id
            original=data.get('raw_text') or ''
            if source.startswith('db:') and db.execute("SELECT 1 FROM sqlite_master WHERE name='radio_events'").fetchone():
                event=db.execute('SELECT raw_text,text FROM radio_events WHERE id=?',(int(source[3:]),)).fetchone()
                if event: original=event[0] if event[0] is not None else (original or event[1])
            elif source.startswith('mem:') and db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_recent_phrases'").fetchone():
                phrase=db.execute('SELECT original_text FROM telegram_recent_phrases WHERE token=?',(source[4:],)).fetchone()
                if phrase: original=phrase[0]
            corrected=reference if label=='speech' and reference else original
            report=save_word_feedback_report(db_path,source,original,corrected,db=db)
            db.execute('''CREATE TABLE IF NOT EXISTS archive_learning_state (
                record_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('''INSERT INTO archive_learning_state VALUES (?,?,?,?) ON CONFLICT(record_id)
                DO UPDATE SET source_id=excluded.source_id, revision=excluded.revision,updated_at=excluded.updated_at''',
                       (record_id,source,revision,time.time()))
            return {'status':'synced','source':source,'candidates':len(report['candidates'])}
    except PermissionError:
        return {'status':'failed','reason':'permission'}
    except Exception:
        logging.getLogger('radio').exception('Не вдалося синхронізувати навчання запису архіву')
        return {'status':'failed'}


def learn_saved_archive(db_path, store, actor, *, progress=lambda *_:None):
    """Явний запуск із панелі: перед голосами — окрема перевірена копія основної БД."""
    import os
    import sqlite3
    import uuid
    from pathlib import Path
    from services.private_files import private_open
    with get_db(db_path) as db: _require_permission(db,actor,'dictionaries.write')
    backups=Path(db_path).resolve().parent/'.local-backups';backups.mkdir(mode=0o700,exist_ok=True)
    backup=backups/('events-before-archive-learning-'+uuid.uuid4().hex+'.db')
    os.close(private_open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY))
    try:
        with closing(read_only(db_path)) as source,sqlite3.connect(backup) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Резервна копія не пройшла перевірку')
    except BaseException:
        backup.unlink(missing_ok=True);raise
    ids=[]
    if (store.root/'index.sqlite3').is_file():
        with closing(read_only(store.root/'index.sqlite3')) as archive:
            ids=[r[0] for r in archive.execute("SELECT id FROM recordings WHERE reference IS NOT NULL AND label='speech' ORDER BY created,id")]
    totals={'synced':0,'failed':0,'missing':0,'candidates':0,'backup':backup.name}
    for number,record_id in enumerate(ids,1):
        outcome=sync_archive_learning(db_path,store,record_id,actor)
        if outcome.get('reason')=='permission': raise PermissionError('Доступ адміністратора більше не дійсний')
        totals[outcome['status']]+=1;totals['candidates']+=outcome.get('candidates',0)
        progress(number,len(ids))
    return totals
