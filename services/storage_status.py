"""Лише читання: обсяг і умови штатної ротації, без запуску очищення."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import stat
import time

from core.log_security import LOG_MAX_BYTES, LOG_BACKUP_COUNT
from services.db_backup import DEFAULT_KEEP, AUTO_RE
from services.diagnostic_history import FILE_RE


def _files(paths):
    result = []
    for path in paths:
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                result.append((path, info))
        except OSError:
            pass
    return result


def audio_status(store, now):
    index = store.root / 'index.sqlite3'
    empty = {'available':False, 'retention_days':store.retention_days, 'max_bytes':store.max_bytes}
    if not index.is_file() or index.is_symlink() or store.root.is_symlink():
        return empty
    from services.recordings import PRUNE_TIER_SQL, PRUNE_TIERS, plan_prune
    try:
        with closing(sqlite3.connect(index.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            records = db.execute(f'SELECT id,created,bytes,reference IS NOT NULL,{PRUNE_TIER_SQL} FROM recordings').fetchall()
    except sqlite3.Error:
        return empty
    rows = sorted(((created, size, reviewed) for _, created, size, reviewed, _ in records), reverse=True)
    total = sum(row[1] for row in rows)
    protected = sum(row[1] for row in rows if row[2])
    unreviewed = [row for row in rows if not row[2]]
    # Те саме рішення, що й prune(), але без видалення файлів і змін у SQLite.
    plan = plan_prune(records, cutoff=now-store.retention_days*86400, max_bytes=store.max_bytes)
    due_count, due_bytes = len(plan), sum(size for _, size, _ in plan)
    tiers = {name: {'count': 0, 'bytes': 0} for name in PRUNE_TIERS}
    for _, _, size, reviewed, tier in records:
        if not reviewed:
            tiers[PRUNE_TIERS[tier]]['count'] += 1
            tiers[PRUNE_TIERS[tier]]['bytes'] += size
    recent = [r for r in rows if r[0] >= now-3600]
    rate = sum(r[1] for r in recent)/3600
    remaining = max(0,store.max_bytes-total)
    oldest = min((r[0] for r in unreviewed), default=None)
    metadata = _files([index,Path(str(index)+'-wal'),Path(str(index)+'-shm')])
    return {**empty, 'available':True, 'count':len(rows), 'bytes':total,
            'protected_count':sum(bool(r[2]) for r in rows), 'protected_bytes':protected,
            'unreviewed_count':len(unreviewed), 'unreviewed_bytes':total-protected,
            'remaining_bytes':remaining, 'metadata_bytes':sum(i.st_size for _,i in metadata),
            'oldest_unreviewed_at':oldest,
            'next_age_expiry_at':oldest+store.retention_days*86400 if oldest is not None else None,
            'due_count':due_count, 'due_bytes':due_bytes, 'unreviewed_by_group':tiers,
            'incoming_bytes_last_hour':sum(r[1] for r in recent),
            'estimated_full_at':now+remaining/rate if remaining and rate and len(recent)>=5 else None}


def log_status(path):
    path = Path(path)
    active = _files([path])
    archives = _files([path.with_name(f'{path.stem}-{i}{path.suffix}') for i in range(1,LOG_BACKUP_COUNT+1)])
    size = sum(i.st_size for _,i in active)
    return {'name':path.name, 'bytes':size+sum(i.st_size for _,i in archives), 'active_bytes':size,
            'max_file_bytes':LOG_MAX_BYTES, 'max_archives':LOG_BACKUP_COUNT, 'archive_count':len(archives),
            'max_total_bytes':LOG_MAX_BYTES*(LOG_BACKUP_COUNT+1),
            'remaining_to_rotation_bytes':max(0,LOG_MAX_BYTES-size),
            'rotations_until_oldest_removed':LOG_BACKUP_COUNT-len(archives)+1 if archives else None}


def storage_status(db_path, store, history, backup_dir, *, now=None):
    now = time.time() if now is None else now
    root = Path(db_path).resolve().parent
    usage = shutil.disk_usage(root)
    diagnostic_files = _files(p for p in history.directory.glob('*.json') if FILE_RE.fullmatch(p.name)) if not history.directory.is_symlink() else []
    automatic = _files(p for p in Path(backup_dir).glob('*.db') if AUTO_RE.fullmatch(p.name)) if not Path(backup_dir).is_symlink() else []
    manual = _files(p for p in Path(backup_dir).glob('*.db') if not AUTO_RE.fullmatch(p.name)) if not Path(backup_dir).is_symlink() else []
    return {'updated_at':now, 'disk':{'total_bytes':usage.total,'free_bytes':usage.free},
            'audio':audio_status(store,now),
            'logs':[log_status(root/'radio.log'),log_status(root/'launcher.log')],
            'diagnostics':{'count':len(diagnostic_files),'bytes':sum(i.st_size for _,i in diagnostic_files),
                           'retention_days':history.retention_days,'max_sessions':history.max_sessions,'max_bytes':history.max_bytes,
                           'next_age_expiry_at':min((i.st_mtime for _,i in diagnostic_files),default=now)+history.retention_days*86400 if diagnostic_files else None},
            'backups':{'automatic_count':len(automatic),'automatic_bytes':sum(i.st_size for _,i in automatic),
                       'keep':DEFAULT_KEEP,'manual_count':len(manual),'manual_bytes':sum(i.st_size for _,i in manual)}}
