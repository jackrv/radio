"""Налаштування розпізнавання, що діють після перезапуску приймача."""
import json
import config
from services.sqlite_connection import get_db
from services.user_store import _require_permission
from services.management.common import ManagementConflict


SETTING_KEYS = ("USE_LLM", "VAD_LEVEL", "SILENCE_END_MS", "MIN_SPEECH_MS", "MAX_SEGMENT_S")


def _settings(db):
    db.execute("""CREATE TABLE IF NOT EXISTS recognition_settings (
        id INTEGER PRIMARY KEY CHECK (id=1), values_json TEXT NOT NULL, revision INTEGER NOT NULL
    )""")
    row = db.execute("SELECT values_json, revision FROM recognition_settings WHERE id=1").fetchone()
    values = {key: getattr(config, key) for key in SETTING_KEYS}
    if row:
        values.update(json.loads(row[0]))
    return {"values": values, "revision": row[1] if row else 0,
            "requires_restart": True, "whisper_model": config.MODEL_REPO,
            "correction_model": config.LLM_REPO, "language": config.LANGUAGE}


def read_settings(db_path):
    with get_db(db_path) as db:
        return _settings(db)


def write_settings(db_path, actor_id, values, revision):
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "settings.write")
        if _settings(db)["revision"] != revision:
            raise ManagementConflict("Налаштування змінилися. Онови сторінку.")
        db.execute("""INSERT INTO recognition_settings (id, values_json, revision) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json, revision=excluded.revision
        """, (json.dumps(values), revision + 1))
        return _settings(db)


def apply_startup_settings(db_path, target):
    settings = read_settings(db_path)["values"]
    for key in SETTING_KEYS:
        setattr(target, key, settings[key])
