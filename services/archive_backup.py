"""Перевірені локальні копії індексу й WAV. Ніколи не очищає робочий архів."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from services.private_files import private_open
from services.sqlite_connection import read_only

ID = re.compile(r'[0-9a-f]{32}\Z')


def inventory(root, *, now=None, grace_s=3600):
    import time
    root = Path(root)
    if root.is_symlink() or (root/'index.sqlite3').is_symlink(): raise ValueError('Архів не може бути символічним посиланням')
    now = time.time() if now is None else now
    with closing(read_only(root / 'index.sqlite3')) as db:
        ids = {r[0] for r in db.execute('SELECT id FROM recordings')}
    files={}
    for path in root.glob('*.wav'):
        if not ID.fullmatch(path.stem): continue
        try: info=path.lstat()
        except FileNotFoundError: continue
        if stat.S_ISREG(info.st_mode): files[path.stem]=info
    orphans = [i for i, info in files.items() if i not in ids and info.st_mtime < now-grace_s]
    return {'indexed': len(ids), 'wav_files': len(files), 'missing': sorted(ids-files.keys()),
            'orphans': sorted(orphans), 'orphan_bytes': sum(files[i].st_size for i in orphans),
            'grace_s': grace_s}


def _copy(source, target):
    digest = hashlib.sha256()
    with os.fdopen(os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as src:
        with os.fdopen(private_open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as out:
            for chunk in iter(lambda: src.read(1024*1024), b''):
                out.write(chunk); digest.update(chunk)
            out.flush(); os.fsync(out.fileno())
    return {'bytes': Path(target).stat().st_size, 'sha256': digest.hexdigest()}


def create_archive_backup(root, destination, *, progress=lambda *_: None):
    root, destination = Path(root), Path(destination)
    if root.is_symlink() or (root / 'index.sqlite3').is_symlink():
        raise ValueError('Символічні посилання архіву не підтримуються')
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination.is_symlink(): raise ValueError('Каталог копій не може бути посиланням')
    destination.chmod(0o700)
    stage = Path(tempfile.mkdtemp(prefix='.building-', dir=destination))
    try:
        index = stage / 'index.sqlite3'
        os.close(private_open(index))
        with closing(read_only(root / 'index.sqlite3')) as source, closing(sqlite3.connect(index)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Індекс архіву не пройшов перевірку цілісності')
            rows = target.execute('SELECT id,bytes FROM recordings ORDER BY id').fetchall()
        needed = sum(size for _, size in rows) + index.stat().st_size
        if shutil.disk_usage(destination).free < needed + 100*1024**2:
            raise ValueError('Недостатньо вільного місця для повної копії архіву й запасу 100 MiB')
        manifest = {'version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(), 'files': {}}
        for number, (record_id, _) in enumerate(rows, 1):
            if not ID.fullmatch(record_id): raise ValueError('Некоректний ID в індексі')
            # Якщо prune випередив копіювання, вся незавершена копія відхиляється.
            # Неповний корпус ніколи не публікується як успішна резервна копія.
            manifest['files'][record_id + '.wav'] = _copy(root / (record_id+'.wav'), stage / (record_id+'.wav'))
            progress(number, len(rows))
        manifest['files']['index.sqlite3'] = {'bytes': index.stat().st_size,
                                            'sha256': hashlib.sha256(index.read_bytes()).hexdigest()}
        with os.fdopen(private_open(stage / 'manifest.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY), 'w') as out:
            json.dump(manifest, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
        verify_archive_backup(stage)
        name = 'archive-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        os.replace(stage, destination / name)
        return {'name': name, 'records': len(rows), 'bytes': needed}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_archive_backup(root):
    root = Path(root)
    if root.is_symlink(): raise ValueError('Каталог копії не може бути посиланням')
    with os.fdopen(os.open(root / 'manifest.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))) as src:
        manifest = json.load(src)
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('Непідтримуваний manifest архіву')
    files = manifest['files']
    if 'index.sqlite3' not in files: raise ValueError('Немає індексу в manifest')
    for name, metadata in files.items():
        if name != 'index.sqlite3' and not (name.endswith('.wav') and ID.fullmatch(name[:-4])):
            raise ValueError('Некоректне ім’я файла в manifest')
        digest = hashlib.sha256(); size = 0
        with os.fdopen(os.open(root / name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as src:
            for chunk in iter(lambda: src.read(1024*1024), b''): digest.update(chunk); size += len(chunk)
        if size != metadata['bytes'] or digest.hexdigest() != metadata['sha256']:
            raise ValueError('Файл резервної копії пошкоджено')
    with closing(read_only(root / 'index.sqlite3')) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Індекс пошкоджено')
        ids = {r[0]+'.wav' for r in db.execute('SELECT id FROM recordings')}
    if set(files) != ids | {'index.sqlite3'}: raise ValueError('Manifest не відповідає індексу')
    return {'records': len(ids), 'verified': True}


def restore_archive_backup(source, destination):
    """Відновити лише в НОВУ теку: робочий корпус неможливо перезаписати цим методом."""
    source, destination = Path(source), Path(destination)
    verify_archive_backup(source)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        manifest = json.loads((source / 'manifest.json').read_text())
        for name in manifest['files']: _copy(source / name, destination / name)
        _copy(source / 'manifest.json', destination / 'manifest.json')
        return verify_archive_backup(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise
