"""Разове очищення архіву; лише явний виклик функцій.

Знімки SQLite + hardlink незмінних WAV до видалення. Резервна копія зберігає
їхні блоки диска: це очищення активного архіву, не обіцянка звільнення диска.
"""
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from services.recordings import ID, VIEWS
from services.sqlite_connection import read_only


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as out:
        os.chmod(path, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())


def _snapshot(source, target):
    deadline = time.monotonic() + 30

    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('Перевищено час створення знімка SQLite')

    with closing(read_only(source, timeout=2)) as original, closing(sqlite3.connect(target)) as copied:
        target.chmod(0o600)
        original.backup(copied, pages=256, sleep=.02, progress=progress)
        if copied.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Копія SQLite не пройшла перевірку')


def prepare(archive_root, events_path, backup_parent, *, include_reviewed_filtered=False,
            include_orphan_uncorrected=False, backup_audio=True):
    root = Path(archive_root).resolve()
    if Path(archive_root).is_symlink() or (root / 'index.sqlite3').is_symlink():
        raise ValueError('Символічні посилання не підтримуються')
    parent = Path(backup_parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError('Каталог копій не може бути посиланням')
    parent.chmod(0o700)
    backup = parent / ('archive-cleanup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700)
    if backup_audio:
        (backup / 'audio').mkdir(mode=0o700)
    # До будь-якого видалення — обидві перевірені копії.
    _snapshot(Path(events_path), backup / 'events.db')
    _snapshot(root / 'index.sqlite3', backup / 'index.sqlite3')
    filtered = VIEWS['filtered'][1]
    condition = f'reference IS NULL OR ({filtered})' if include_reviewed_filtered else 'reference IS NULL'
    with closing(read_only(backup / 'index.sqlite3')) as db:
        rows = db.execute(f'SELECT id,revision,bytes FROM recordings WHERE {condition} ORDER BY id').fetchall()
        reviewed = db.execute(f'SELECT count(*) FROM recordings WHERE reference IS NOT NULL AND ({condition})').fetchone()[0]
        all_count = db.execute('SELECT count(*) FROM recordings').fetchone()[0]
    missing = []
    for record_id, _, _ in rows:
        if not ID.fullmatch(record_id):
            raise ValueError('Некоректний ID в індексі')
        source = root / (record_id + '.wav')
        try:
            info = source.lstat()
        except FileNotFoundError:
            missing.append(record_id)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Аудіо має бути звичайним файлом')
        # WAV у RecordingStore незмінні. Hardlink не потребує читання гігабайтів
        # і дозволяє відновлення після видалення імені з активного архіву.
        if backup_audio:
            os.link(source, backup / 'audio' / source.name, follow_symlinks=False)
    manifest = {'version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'archive_root': str(root), 'events_path': str(Path(events_path).resolve()),
                'include_reviewed_filtered': include_reviewed_filtered,
                'include_orphan_uncorrected': include_orphan_uncorrected,
                'initial_total': all_count, 'planned': len(rows), 'planned_reviewed': reviewed,
                'planned_bytes': sum(row[2] for row in rows), 'missing_audio': missing,
                'audio_backup': 'hardlink-immutable-wav' if backup_audio else 'none',
                'candidates': [{'id': rid, 'revision': revision} for rid, revision, _ in rows]}
    _write_json(backup / 'manifest.json', manifest)
    return backup


def discard_audio_backup(backup):
    """Явна відмова власника від аудіокопії; SQLite snapshots лишаються."""
    backup = Path(backup)
    path = backup / 'manifest.json'
    manifest = json.loads(path.read_text())
    if manifest.get('version') != 1 or (backup / 'audio').is_symlink():
        raise ValueError('Некоректна копія')
    count = 0
    for item in manifest['candidates']:
        if not ID.fullmatch(item['id']):
            raise ValueError('Некоректний ID у manifest')
        audio = backup / 'audio' / (item['id'] + '.wav')
        if audio.exists():
            audio.unlink()
            count += 1
    manifest['audio_backup'] = 'none'
    temporary = backup / 'manifest-no-audio.json'
    _write_json(temporary, manifest)
    os.replace(temporary, path)
    if (backup / 'audio').exists():
        (backup / 'audio').rmdir()
    return count


def apply(archive_root, backup, *, batch_size=250):
    root, backup = Path(archive_root).resolve(), Path(backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest['archive_root'] != str(root):
        raise ValueError('Копія стосується іншого архіву')
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError('Некоректний розмір пакета')
    # Якщо підготовка не завершилась/аудіокопія зникла, жодного delete.
    known_missing = set(manifest['missing_audio'])
    if manifest.get('audio_backup') not in ('none', 'hardlink-immutable-wav'):
        raise ValueError('Невідомий режим аудіокопії')
    for item in manifest['candidates']:
        if not ID.fullmatch(item['id']):
            raise ValueError('Некоректний ID у manifest')
        if manifest['audio_backup'] != 'none' and item['id'] not in known_missing and not (backup / 'audio' / (item['id'] + '.wav')).is_file():
            raise ValueError('Копія аудіо неповна')
    condition = f"r.reference IS NULL OR ({VIEWS['filtered'][1]})" if manifest['include_reviewed_filtered'] else 'r.reference IS NULL'
    removed = unlinked = recalls = 0
    with closing(sqlite3.connect(root / 'index.sqlite3', timeout=10)) as db:
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('CREATE TEMP TABLE candidates(id TEXT PRIMARY KEY,revision INTEGER NOT NULL)')
        db.executemany('INSERT INTO candidates VALUES(?,?)', [(i['id'], i['revision']) for i in manifest['candidates']])
        db.commit()
        has_recalls = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='recording_recalls'").fetchone())
        while True:
            try:
                db.execute('BEGIN IMMEDIATE')
                batch = [row[0] for row in db.execute(f'''SELECT r.id FROM recordings r JOIN candidates c ON r.id=c.id
                    WHERE r.revision=c.revision AND ({condition}) LIMIT ?''', (batch_size,))]
                if not batch:
                    db.commit()
                    break
                marks = ','.join('?' for _ in batch)
                if has_recalls:
                    recalls += db.execute(f'DELETE FROM recording_recalls WHERE record_id IN ({marks})', batch).rowcount
                deleted = db.execute(f'DELETE FROM recordings WHERE id IN ({marks})', batch).rowcount
                db.commit()
            except BaseException:
                db.rollback()
                raise
            removed += deleted
            # DB commit спочатку: збій unlink залишить лише зайвий файл, не
            # рядок із знищеним аудіо. Аудіокопія є лише за окремим вибором власника.
            for record_id in batch:
                path = root / (record_id + '.wav')
                if path.exists():
                    path.unlink()
                    unlinked += 1
    result = {'removed': removed, 'unlinked_wav': unlinked, 'cancelled_archive_recalls': recalls,
              'not_removed_from_plan': manifest['planned'] - removed,
              'backup': str(backup), 'scope': 'unreviewed-and-reviewed-filtered' if manifest['include_reviewed_filtered'] else 'unreviewed'}
    _write_json(backup / ('result-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'), result)
    return result


def clean_primary(archive_root, backup):
    """Прибрати пов'язані фрази, зберігши словники/навчання/sequence та нові правки.

    Викликати після apply; план фіксується копіями, не розширюється новими подіями.
    Службові адреси Telegram лишаються: цей метод НЕ надсилає HTTP-видалень.
    """
    root, backup = Path(archive_root).resolve(), Path(backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest['archive_root'] != str(root):
        raise ValueError('Копія стосується іншого архіву')
    with closing(read_only(backup / 'index.sqlite3')) as snapshot:
        original_links = dict(snapshot.execute("SELECT id,json_extract(data,'$.fix_id') FROM recordings"))
    candidates = {item['id'] for item in manifest['candidates']}
    with closing(read_only(backup / 'events.db')) as snapshot:
        tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events = {f'db:{row[0]}': tuple(row) for row in snapshot.execute('SELECT * FROM radio_events')} if 'radio_events' in tables else {}
        phrases = {f'mem:{row[0]}': tuple(row) for row in snapshot.execute('SELECT * FROM telegram_recent_phrases')} if 'telegram_recent_phrases' in tables else {}
        corrections = dict(snapshot.execute('SELECT fix_id,revision FROM correction_sources')) if 'correction_sources' in tables else {}
    counts = {'removed_events': 0, 'removed_memory_phrases': 0, 'removed_learning_links': 0,
              'preserved_changed_primary': 0, 'protected_shared_sources': 0}
    with closing(sqlite3.connect(manifest['events_path'], timeout=10)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
            with closing(read_only(root / 'index.sqlite3', timeout=2)) as archive:
                current_links = dict(archive.execute("SELECT id,json_extract(data,'$.fix_id') FROM recordings"))
            removed = candidates - current_links.keys()
            related = {original_links[rid] for rid in removed if original_links.get(rid)}
            protected = {fix for fix in current_links.values() if fix}
            if manifest.get('include_orphan_uncorrected'):
                related |= (events.keys() | phrases.keys()) - set(original_links.values()) - corrections.keys()
            counts['protected_shared_sources'] = len(related & protected)
            related -= protected
            current_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            current_corrections = dict(db.execute('SELECT fix_id,revision FROM correction_sources')) if 'correction_sources' in current_tables else {}
            for fix_id in related:
                source = events if fix_id.startswith('db:') else phrases
                saved = source.get(fix_id)
                if saved is None:
                    continue  # джерело не потрапило до snapshot — видаляти не можна
                is_event = fix_id.startswith('db:')
                table, column = ('radio_events', 'id') if is_event else ('telegram_recent_phrases', 'token')
                value = int(fix_id[3:]) if is_event else fix_id[4:]
                current = db.execute(f'SELECT * FROM {table} WHERE {column}=?', (value,)).fetchone()
                if current is None:
                    continue
                if tuple(current) != saved or current_corrections.get(fix_id) != corrections.get(fix_id):
                    counts['preserved_changed_primary'] += 1
                    continue
                db.execute(f'DELETE FROM {table} WHERE {column}=?', (value,))
                if 'correction_sources' in current_tables:
                    db.execute('DELETE FROM correction_sources WHERE fix_id=?', (fix_id,))
                counts['removed_events' if is_event else 'removed_memory_phrases'] += 1
            if 'archive_learning_state' in current_tables:
                for rid in removed:
                    counts['removed_learning_links'] += db.execute('DELETE FROM archive_learning_state WHERE record_id=?', (rid,)).rowcount
            db.commit()
        except BaseException:
            db.rollback()
            raise
    _write_json(backup / ('primary-result-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'), counts)
    return counts
