"""Реальні POSIX-блокування; тільки тимчасові SQLite й окремий процес-зонд."""
import os
import sqlite3
import subprocess
import sys

import pytest

from services.private_files import protect_existing
from services.sqlite_connection import get_db

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX record locks')

PROBE = '''import os,fcntl,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,8,120,os.SEEK_SET)
 print('UNLOCKED')
except BlockingIOError: print('LOCKED')
finally: os.close(fd)
'''


def probe(path):
    return subprocess.check_output([sys.executable, '-c', PROBE, str(path)+'-shm'], text=True).strip()


@pytest.mark.parametrize('operation', ['permissions', 'second_connection'])
def test_other_connections_never_release_active_sqlite_writer_lock(tmp_path, operation):
    path = tmp_path / 'events.db'
    with get_db(path) as db:
        db.execute('CREATE TABLE t(value)')
    with get_db(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('INSERT INTO t VALUES(1)')
        assert probe(path) == 'LOCKED'
        if operation == 'permissions':
            protect_existing(path)
            protect_existing(str(path)+'-wal')
            protect_existing(str(path)+'-shm')
        else:
            with get_db(path) as reader:
                reader.execute('SELECT 1').fetchone()
        assert probe(path) == 'LOCKED'
    with get_db(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT COUNT(*) FROM t').fetchone() == (1,)


def test_metadata_protection_does_not_follow_symlinks(tmp_path):
    target = tmp_path / 'target'; target.write_text('synthetic'); target.chmod(0o644)
    link = tmp_path / 'link'; link.symlink_to(target)
    with pytest.raises(OSError):
        protect_existing(link)
    assert target.stat().st_mode & 0o777 == 0o644


def test_sidecar_can_disappear_between_stat_and_chmod(tmp_path, monkeypatch):
    path = tmp_path / 'events.db-shm'; path.touch()
    def removed(*args, **kwargs):
        path.unlink()
        raise FileNotFoundError(str(path))
    monkeypatch.setattr(os, 'chmod', removed)
    protect_existing(path)
