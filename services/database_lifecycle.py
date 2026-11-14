"""Локальне налаштування, експорт і контрольоване очищення SQLite."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time

from callsigns import Callsigns
from database import (
    DICTIONARY_TYPES, format_dictionary_file, init_db as init_dictionaries,
    import_dictionaries, parse_dictionary_file,
)
from services.event_store import init_db as init_events
from services.miniapp_management import validate_dictionary
from services.sqlite_connection import get_db
from services.user_store import init_users_db


def validate_sections(sections, filename):
    """Ті самі правила, що й у Mini App: імпортоване завжди можна зберегти без змін.

    Повідомлення містить розділ і номер запису, але не сам текст словника.
    """
    for kind, rows in sections.items():
        entries = [{"raw_line": line, "enabled": enabled} for line, enabled in rows]
        for number, entry in enumerate(entries, start=1):
            try:
                # Формат одного запису; вимогу «є позивні» перевіряємо окремо нижче.
                validate_dictionary([entry], "commands")
            except ValueError as exc:
                raise ValueError(f"{filename}, розділ [{kind}], запис {number}: {exc}") from None
        try:
            validate_dictionary(entries, kind)
        except ValueError as exc:
            raise ValueError(f"{filename}, розділ [{kind}]: {exc}") from None


def initialize_database(db_path, source_path, *, delete_source=False):
    """Імпорт запускається явно; шаблон без записів не є робочим словником."""
    source_path = Path(source_path)
    sections = parse_dictionary_file(source_path.read_text(encoding="utf-8-sig"))
    validate_sections(sections, source_path.name)
    enabled = [line for line, on in sections["callsigns"] if on]
    if not Callsigns.from_lines(enabled).names:
        raise ValueError(f"У розділі [callsigns] файлу {source_path.name} немає увімкнених позивних")
    init_dictionaries(db_path)
    init_events(db_path)
    init_users_db(db_path)
    return import_dictionaries(source_path, db_path, delete_source=delete_source)


def _export(db, destination):
    """Записує всі словники в один новий файл; наявний файл не перезаписується."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Експорт зупинено: {destination.name} вже існує; його не перезаписано")
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        kind: [(line, bool(enabled)) for line, enabled in db.execute(
            "SELECT raw_line,enabled FROM dictionary_entries WHERE dictionary_type=? ORDER BY line_number", (kind,))]
        for kind in DICTIONARY_TYPES
    }
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(format_dictionary_file(entries))
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {kind: len(rows) for kind, rows in entries.items()}


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


# Якщо поточна база нечитабельна, невідомо, скільки подій було після копії:
# зсуваємо лічильник із запасом, щоб старі посилання db:<id> не влучили в нові події.
RESTORE_SAFE_JUMP = 1_000_000


def _floors(conn):
    """(наступний ID події, наступний offset Telegram) для відкритої бази."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    next_id = 1
    if "radio_events" in tables:
        next_id = max(next_id, conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM radio_events").fetchone()[0])
    if "radio_event_sequence" in tables:
        row = conn.execute("SELECT next_id FROM radio_event_sequence WHERE id=1").fetchone()
        if row:
            next_id = max(next_id, row[0])
    offset = None
    if "telegram_poll_state" in tables:
        row = conn.execute("SELECT next_offset FROM telegram_poll_state WHERE id=1").fetchone()
        offset = row[0] if row else None
    return next_id, offset


def restore_database(db_path, backup_path, backup_dir, *, lock_path=None, now=None):
    """Відновлює events.db з копії, не повторюючи вже виданих ID подій."""
    from core.instance_lock import acquire_instance_lock

    db_path = Path(db_path).resolve()
    backup_path = Path(backup_path).resolve()
    if not backup_path.is_file():
        raise FileNotFoundError("Файл резервної копії не знайдено")
    if backup_path == db_path:
        raise ValueError("Копія і робоча база — той самий файл")

    lock = acquire_instance_lock(lock_path or db_path.parent / ".radio345.lock")
    if lock is None:
        raise RuntimeError("Спочатку зупиніть приймач: відновлення під час роботи заборонено")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    folder = Path(backup_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = db_path.parent / f".events-restore-{stamp}.tmp"
    try:
        with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as source:
            if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Резервна копія пошкоджена")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)

        live_floor = live_offset = None
        live_readable = False
        if db_path.is_file():
            try:
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as live:
                    if live.execute("PRAGMA quick_check").fetchone() == ("ok",):
                        live_floor, live_offset = _floors(live)
                        live_readable = True
            except sqlite3.Error:
                live_readable = False

        with closing(sqlite3.connect(temporary)) as restored:
            backup_floor, backup_offset = _floors(restored)
            jumped = db_path.is_file() and not live_readable
            next_id = max(backup_floor, live_floor or 0)
            if jumped:
                next_id = backup_floor + RESTORE_SAFE_JUMP
            restored.execute("CREATE TABLE IF NOT EXISTS radio_event_sequence (id INTEGER PRIMARY KEY CHECK(id=1), next_id INTEGER NOT NULL)")
            restored.execute("INSERT INTO radio_event_sequence VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_id=excluded.next_id", (next_id,))
            offsets = [value for value in (backup_offset, live_offset) if value is not None]
            if offsets:
                restored.execute("CREATE TABLE IF NOT EXISTS telegram_poll_state (id INTEGER PRIMARY KEY CHECK(id=1), next_offset INTEGER NOT NULL)")
                restored.execute("INSERT INTO telegram_poll_state (id,next_offset) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_offset=excluded.next_offset", (max(offsets),))
            restored.commit()
            if restored.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Відновлена база не пройшла перевірку цілісності")

        previous = None
        if db_path.is_file():
            if live_readable:
                previous = folder / f"events-before-restore-{stamp}.db"
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as live:
                    fd = os.open(previous, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    with closing(sqlite3.connect(previous)) as copy:
                        live.backup(copy)
            else:
                previous = folder / f"events-broken-{stamp}.db"
                os.replace(db_path, previous)
        # Старий WAL не можна залишати поруч із новою базою: SQLite застосує його.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                if live_readable:
                    sidecar.unlink()
                else:
                    os.replace(sidecar, folder / f"events-broken-{stamp}.db{suffix}")
        os.replace(temporary, db_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(lock)
    return {"restored_from": str(backup_path), "next_event_id": next_id,
            "previous_database": str(previous) if previous else None, "ids_jumped": jumped}
