"""Стійкий намір відкликати немовлення: індекс архіву → черга бота.

20 с на швидке скасування мітки; жодного HTTP у процесі панелі.
Старі мітки не перебираються: намір створюється лише під час нового збереження.
"""
import json
import logging
import sqlite3
import time

from shared.telegram_media_store import FIX_RE
from shared.telegram_recall import request_delete

GRACE_S = 20
NON_SPEECH = ('noise', 'notification', 'room')


def stage(db, record_id, *, now=None):
    """Викликається всередині транзакції розмітки. Інша мітка скасовує очікування."""
    db.execute('''CREATE TABLE IF NOT EXISTS recording_recalls (
        record_id TEXT PRIMARY KEY, fix_id TEXT NOT NULL,
        revision INTEGER NOT NULL, due_at REAL NOT NULL)''')
    row = db.execute('SELECT label,data,revision FROM recordings WHERE id=?', (record_id,)).fetchone()
    fix_id = (json.loads(row[1]).get('fix_id') or '') if row else ''
    if row and row[0] in NON_SPEECH and isinstance(fix_id, str) and FIX_RE.fullmatch(fix_id):
        db.execute('''INSERT INTO recording_recalls VALUES (?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET fix_id=excluded.fix_id,
            revision=excluded.revision,due_at=excluded.due_at''',
                   (record_id, fix_id, row[2], (time.time() if now is None else now) + GRACE_S))
    else:
        db.execute('DELETE FROM recording_recalls WHERE record_id=?', (record_id,))


def status(store, record_id, *, now=None):
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='recording_recalls'").fetchone()
        row = db.execute('SELECT due_at FROM recording_recalls WHERE record_id=?', (record_id,)).fetchone() if exists else None
    return ({'status': 'scheduled', 'after_seconds': max(0, row[0] - (time.time() if now is None else now))}
            if row else None)


def dispatch(db_path, store, *, now=None, limit=20):
    """Передача між БД повторюється після збою, не скидає exhausted-спроби бота."""
    now = time.time() if now is None else now
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recording_recalls'").fetchone():
            return 0
        rows = db.execute('''SELECT q.record_id,q.fix_id,q.revision,r.label,r.data,r.revision
            FROM recording_recalls q LEFT JOIN recordings r ON r.id=q.record_id
            WHERE q.due_at<=? ORDER BY q.due_at LIMIT ?''', (now, limit)).fetchall()
    transferred = 0
    for rid, fix_id, revision, label, encoded, current_revision in rows:
        try:
            current_fix = (json.loads(encoded).get('fix_id') or '') if encoded else None
            current = label in NON_SPEECH and current_revision == revision and current_fix == fix_id
            if current:
                request_delete(db_path, fix_id, now=now, retry_exhausted=False)
            # Ревізія оберігає новий намір від підтвердження старої операції.
            with store.connect() as db:
                db.execute('DELETE FROM recording_recalls WHERE record_id=? AND revision=?', (rid, revision))
            transferred += int(current)
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError) as exc:
            logging.getLogger('radio').warning('Відкликання немовлення з архіву відкладено (%s)', type(exc).__name__)
    return transferred
