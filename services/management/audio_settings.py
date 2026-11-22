"""Звукові пристрої приймача, вибрані в панелі; діють після перезапуску приймача.

Зберігається лише відмінне від config.py. Змінна в ``.env`` (оточенні процесу)
має пріоритет над панеллю — так само, як над config.py (services/audio_devices.py).
"""
import json
import os

import config
from services.management.common import ManagementConflict
from services.sqlite_connection import get_db
from services.user_store import _require_permission

KEYS = ("RADIO_INPUT_DEVICE", "RADIO_OUTPUT_DEVICE")
MAX_NAME = 200


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS audio_device_settings (
        id INTEGER PRIMARY KEY CHECK (id=1), values_json TEXT NOT NULL, revision INTEGER NOT NULL)""")
    row = db.execute("SELECT values_json, revision FROM audio_device_settings WHERE id=1").fetchone()
    defaults = {key: (getattr(config, key, "") or "").strip() for key in KEYS}
    values = dict(defaults)
    if row:
        values.update({k: v for k, v in json.loads(row[0]).items() if k in KEYS})
    return {"values": values, "revision": row[1] if row else 0, "defaults": defaults,
            "env_override": [key for key in KEYS if os.environ.get(key) is not None]}


def clean_name(value):
    if not isinstance(value, str):
        raise ValueError("Назва пристрою має бути текстом")
    value = value.strip()
    if len(value) > MAX_NAME or any(ord(ch) < 32 for ch in value):
        raise ValueError("Некоректна назва пристрою")
    return value


def read_audio_settings(db_path):
    with get_db(db_path) as db:
        return _table(db)


def write_audio_settings(db_path, actor_id, values, revision):
    values = {key: clean_name(values.get(key, "")) for key in KEYS}
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "settings.write")
        current = _table(db)
        if current["revision"] != revision:
            raise ManagementConflict("Налаштування пристроїв змінилися. Оновіть сторінку.")
        changed = {key: value for key, value in values.items() if value != current["defaults"][key]}
        db.execute("""INSERT INTO audio_device_settings (id, values_json, revision) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json, revision=excluded.revision""",
                   (json.dumps(changed, ensure_ascii=False), revision + 1))
        return _table(db)


def apply_audio_settings(db_path, target):
    """Під час старту приймача: вибір панелі → атрибути config (``.env`` усе одно важливіший)."""
    for key, value in read_audio_settings(db_path)["values"].items():
        setattr(target, key, value)
