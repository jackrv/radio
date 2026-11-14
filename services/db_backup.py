"""Щоденна автоматична резервна копія events.db.

Копія робиться штатним SQLite Backup API з одного знімка читання, тож
записи приймача під час копіювання не порушують узгодженість. Кожна копія
перевіряється (integrity_check і кількість рядків у кожній таблиці) й лише
тоді отримує остаточну назву. Зберігаються останні ``keep`` автоматичних
копій; інші файли в каталозі (наприклад, копії перед reset) не чіпаються.
"""

from contextlib import closing
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from core.diagnostics import runtime

AUTO_PREFIX = "events-auto-"
AUTO_RE = re.compile(r"events-auto-(\d{8}T\d{6}Z)\.db\Z")
TEMP_RE = re.compile(r"\.events-auto-\d{8}T\d{6}Z\.db\.tmp\Z")
DEFAULT_KEEP = 7
BACKUP_INTERVAL_S = 24 * 3600
STALE_TEMP_S = 3600


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def create_backup(db_path, backup_dir, *, keep=DEFAULT_KEEP, now=None):
    """Створює перевірену копію й повертає шлях до неї."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError("Робочу SQLite не знайдено")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final = backup_dir / f"{AUTO_PREFIX}{stamp}.db"
    temporary = backup_dir / f".{final.name}.tmp"
    if final.exists():
        raise FileExistsError("Копія з такою міткою часу вже існує")

    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=15.0)) as source:
            source.execute("PRAGMA busy_timeout=10000")
            source.execute("BEGIN")  # один знімок і для копії, і для звірки
            tables = [row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            expected = {t: source.execute(f"SELECT COUNT(*) FROM {_quoted(t)}").fetchone()[0] for t in tables}
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("Резервна копія не пройшла перевірку цілісності")
                for table, count in expected.items():
                    if target.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0] != count:
                        raise RuntimeError("Резервна копія не відповідає базі")
            source.rollback()
        os.replace(temporary, final)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    prune_backups(backup_dir, keep)
    return final


def auto_backups(backup_dir):
    """Автоматичні копії від найновішої до найстарішої."""
    try:
        names = [p for p in Path(backup_dir).iterdir() if AUTO_RE.fullmatch(p.name)]
    except FileNotFoundError:
        return []
    return sorted(names, key=lambda p: p.name, reverse=True)


def prune_backups(backup_dir, keep=DEFAULT_KEEP):
    """Видаляє лише зайві автоматичні копії та покинуті тимчасові файли."""
    if keep < 1:
        raise ValueError("Потрібно зберігати принаймні одну копію")
    for old in auto_backups(backup_dir)[keep:]:
        old.unlink(missing_ok=True)
    # Лише покинуті тимчасові файли: свіжий може належати копії, що ще пишеться.
    cutoff = time.time() - STALE_TEMP_S
    for path in Path(backup_dir).iterdir():
        if TEMP_RE.fullmatch(path.name):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def latest_backup_age(backup_dir, now=None):
    """Вік найновішої автоматичної копії в секундах або None."""
    backups = auto_backups(backup_dir)
    if not backups:
        return None
    taken = datetime.strptime(AUTO_RE.fullmatch(backups[0].name).group(1), "%Y%m%dT%H%M%SZ")
    taken = taken.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - taken).total_seconds()


def backup_if_due(db_path, backup_dir, *, keep=DEFAULT_KEEP, interval_s=BACKUP_INTERVAL_S, log=None, now=None):
    """Робить копію, якщо остання старша за ``interval_s``. Ніколи не кидає помилку."""
    log = log or logging.getLogger("radio")
    try:
        age = latest_backup_age(backup_dir, now)
        if age is not None and age < interval_s:
            return None
        path = create_backup(db_path, backup_dir, keep=keep, now=now)
    except (OSError, sqlite3.Error, RuntimeError):
        runtime.count("backup.failed")
        log.exception("Не вдалося створити автоматичну резервну копію бази")
        return None
    runtime.count("backup.created")
    log.info("Автоматичну резервну копію бази створено: %s", path.name)
    return path


def start_backup_scheduler(db_path, backup_dir, *, log=None, check_every_s=3600, first_delay_s=300):
    """Фоновий потік: перевіряє раз на годину, чи час робити щоденну копію."""
    stop = threading.Event()

    def loop():
        if stop.wait(first_delay_s):
            return
        while True:
            backup_if_due(db_path, backup_dir, log=log)
            if stop.wait(check_every_s):
                return

    thread = threading.Thread(target=loop, name="radio-db-backup", daemon=True)
    thread.start()
    return stop
