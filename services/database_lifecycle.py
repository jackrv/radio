"""Локальне налаштування, експорт і контрольоване очищення SQLite."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time

from callsigns import Callsigns
from database import DICTIONARY_FILES, init_db as init_dictionaries, import_dictionaries
from services.event_store import init_db as init_events
from services.sqlite_connection import get_db
from services.user_store import init_users_db


def initialize_database(db_path, source_dir, *, delete_sources=False):
    """Імпорт запускається явно; порожні шаблони не є робочими словниками."""
    source_dir = Path(source_dir)
    lines = (source_dir / "callsigns.txt").read_text(encoding="utf-8-sig").splitlines()
    enabled = [line for line in lines if not line.startswith("# @radio345-disabled: ")]
    if not Callsigns.from_lines(enabled).names:
        raise ValueError("Файл callsigns.txt не містить увімкнених позивних")
    init_dictionaries(db_path)
    init_events(db_path)
    init_users_db(db_path)
    return import_dictionaries(source_dir, db_path, delete_sources=delete_sources)


def _export(db, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    paths = [destination / filename for filename in DICTIONARY_FILES.values()]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Експорт зупинено: локальні TXT вже існують; їх не перезаписано")
    created = []
    counts = {}
    try:
        for kind, filename in DICTIONARY_FILES.items():
            rows = db.execute("SELECT raw_line,enabled FROM dictionary_entries WHERE dictionary_type=? ORDER BY line_number", (kind,)).fetchall()
            text = "\n".join(line if enabled else "# @radio345-disabled: " + line for line, enabled in rows)
            path = destination / filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text + ("\n" if rows else ""))
                file.flush()
                os.fsync(file.fileno())
            counts[kind] = len(rows)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return counts


def export_dictionaries(db_path, destination):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        return _export(db, destination)


def reset_database(db_path, destination, backup_dir, *, keep_admins=True):
    """Зберігає словники й перевірену копію перед однією транзакцією DELETE."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError("Робочу SQLite не знайдено")
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if "receiver_runtime" in tables:
            row = db.execute("SELECT state,updated_at FROM receiver_runtime WHERE id=1").fetchone()
            if row and row[0] in ("listening", "paused") and time.time() - row[1] < 15:
                raise RuntimeError("Спочатку зупиніть приймач і API; активний heartbeat забороняє очищення")
        folder = Path(backup_dir)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = folder / ("events-before-reset-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        # A separate read-only connection can back up while this writer reserves
        # the DB. No writes have occurred yet; concurrent writers cannot race reset.
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if list(target.execute("PRAGMA integrity_check")) != [("ok",)]:
                    raise RuntimeError("Резервна копія не пройшла перевірку цілісності")
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    if db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone() != target.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone():
                        raise RuntimeError("Резервна копія не відповідає поточній базі")
        counts = _export(db, destination)
        floor = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM radio_events").fetchone()[0]
        db.execute("CREATE TABLE IF NOT EXISTS radio_event_sequence (id INTEGER PRIMARY KEY CHECK(id=1), next_id INTEGER NOT NULL)")
        db.execute("INSERT INTO radio_event_sequence VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_id=MAX(next_id,excluded.next_id)", (floor,))
        floor = db.execute("SELECT next_id FROM radio_event_sequence WHERE id=1").fetchone()[0]
        for table in sorted(tables, key=lambda value: (value not in ("word_correction_votes", "telegram_user_profiles"), value)):
            if table in ("radio_event_sequence", "telegram_poll_state"):
                continue
            if table == "telegram_users" and keep_admins:
                db.execute("DELETE FROM telegram_users WHERE role<>'admin' OR enabled<>1")
            else:
                quoted = '"' + table.replace('"', '""') + '"'
                db.execute(f"DELETE FROM {quoted}")
        admins = db.execute("SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1").fetchone()[0]
    return {"backup": str(backup), "exported": counts, "active_admins": admins,
            "next_event_id": floor, "telegram_offset_preserved": "telegram_poll_state" in tables}
