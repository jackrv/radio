"""Звірка WAV з індексом аудіоархіву: лише читання, нічого не видаляє."""
import re
import stat
from contextlib import closing
from pathlib import Path

from shared.sqlite_connection import read_only

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
