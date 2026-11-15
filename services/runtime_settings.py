"""Налаштування обробки, що застосовуються без перезапуску приймача.

Початкові значення — з config.py; у SQLite зберігаються лише змінені в локальній
панелі. Приймач перевіряє ревізію не частіше ніж раз на кілька секунд.
Моделі, VAD і MIN_RMS сюди свідомо не входять.
"""

import json
import sqlite3
import time

from services.sqlite_connection import get_db

REFRESH_INTERVAL_S = 2.0

# ключ: (тип, мінімум, максимум, атрибут config із початковим значенням, запасне значення)
FIELDS = {
    "COOLDOWN_S": (float, 0.0, 120.0, "COOLDOWN_S", 3.0),
    "LOG_CALLSIGNS_ONLY": (bool, None, None, "LOG_CALLSIGNS_ONLY", True),
    "GARBAGE_MIN_WORDS": (int, 4, 200, "GARBAGE_MIN_WORDS", 12),
    "GARBAGE_MIN_UNIQUE_RATIO": (float, 0.0, 1.0, "GARBAGE_MIN_UNIQUE_RATIO", 0.25),
    "GARBAGE_MAX_WORDS_PER_S": (float, 1.0, 30.0, "GARBAGE_MAX_WORDS_PER_S", 5.0),
    "GARBAGE_EXTRA_WORDS": (int, 0, 100, "GARBAGE_EXTRA_WORDS", 3),
}


class RuntimeSettingsConflict(Exception):
    pass


def defaults():
    import config
    return {key: kind(getattr(config, attribute, fallback))
            for key, (kind, _, _, attribute, fallback) in FIELDS.items()}


def validate(values):
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValueError("Невідомі налаштування")
    clean = {}
    for key, value in values.items():
        kind, low, high, _, _ = FIELDS[key]
        if kind is bool:
            if type(value) is not bool:
                raise ValueError(f"{key}: потрібне так/ні")
        elif kind is int:
            if type(value) is not int:
                raise ValueError(f"{key}: потрібне ціле число")
        else:
            if type(value) not in (int, float) or value != value:
                raise ValueError(f"{key}: потрібне число")
            value = float(value)
        if low is not None and not low <= value <= high:
            raise ValueError(f"{key}: допустимо від {low:g} до {high:g}")
        clean[key] = value
    return clean


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
        id INTEGER PRIMARY KEY CHECK(id = 1), values_json TEXT NOT NULL,
        revision INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")


def _read(db):
    row = db.execute("SELECT values_json, revision FROM runtime_settings WHERE id = 1").fetchone() \
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_settings'").fetchone() else None
    stored, revision = ({}, 0) if row is None else (json.loads(row[0]), row[1])
    values = defaults()
    try:
        values.update(validate({k: v for k, v in stored.items() if k in FIELDS}))
    except ValueError:
        pass  # пошкоджений запис: безпечні початкові значення
    return values, revision


def read_runtime_settings(db_path):
    with get_db(db_path) as db:
        values, revision = _read(db)
    return {"values": values, "revision": revision, "defaults": defaults(),
            "limits": {k: {"min": low, "max": high, "type": kind.__name__}
                       for k, (kind, low, high, _, _) in FIELDS.items()}}


def write_runtime_settings(db_path, values, expected_revision):
    clean = validate(values)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _ensure(db)
        current, revision = _read(db)
        if revision != expected_revision:
            raise RuntimeSettingsConflict("Налаштування змінено в іншому вікні. Онови сторінку.")
        current.update(clean)
        db.execute("""INSERT INTO runtime_settings (id, values_json, revision) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET values_json = excluded.values_json, revision = excluded.revision,
            updated_at = CURRENT_TIMESTAMP""", (json.dumps(current), revision + 1))
    return {"saved": True, "revision": revision + 1, "values": current}


class LiveRuntimeSettings:
    """Застосовує нові значення через ``apply(values)``; помилка бази — лишаються попередні."""

    def __init__(self, db_path, apply, log=None, *, interval=REFRESH_INTERVAL_S, clock=time.monotonic):
        self.db_path = db_path
        self.apply = apply
        self.log = log
        self.interval = interval
        self.clock = clock
        self.revision = None
        self.values = None
        self._checked = None
        self._failed = False

    def refresh(self, force=False):
        now = self.clock()
        if not force and self._checked is not None and now - self._checked < self.interval:
            return False
        self._checked = now
        try:
            with get_db(self.db_path) as db:
                values, revision = _read(db)
        except (sqlite3.Error, ValueError):
            if self.log is not None and not self._failed:
                self.log.warning("Налаштування обробки недоступні; використовую попередні")
            self._failed = True
            return False
        self._failed = False
        if revision == self.revision and not force:
            return False
        changed = self.revision is not None
        self.apply(values)
        self.values, self.revision = values, revision
        try:
            from core.diagnostics import runtime
            runtime.gauge("runtime_settings.revision", revision)
        except Exception:
            pass
        if self.log is not None and changed:
            self.log.info("Налаштування обробки оновлено без перезапуску (ревізія %d)", revision)
        return True
