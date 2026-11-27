"""Налаштування обробки, що застосовуються без перезапуску приймача.

Значення за замовчуванням — лише в config.py (тут їх не дублюємо); у SQLite
зберігаються тільки ті, що в панелі змінено відносно config.py. Тож зміна
config.py діє для всього, чого оператор не чіпав. Приймач перевіряє ревізію не
частіше ніж раз на кілька секунд. Моделі, мова й VAD — окремо, після перезапуску
(server/management/settings.py).
"""

import json
import sqlite3
import time

from shared.sqlite_connection import get_db

REFRESH_INTERVAL_S = 2.0

# ключ (і атрибут config із початковим значенням): (тип, мінімум або варіанти, максимум)
FIELDS = {
    "TELEGRAM_AUTO_DELETE_HOURS": (int, 0, 47),
    "COOLDOWN_S": (float, 0.0, 120.0),
    "LOG_CALLSIGNS_ONLY": (bool, None, None),
    "GARBAGE_MIN_WORDS": (int, 4, 200),
    "GARBAGE_MIN_UNIQUE_RATIO": (float, 0.0, 1.0),
    "GARBAGE_MAX_WORDS_PER_S": (float, 1.0, 30.0),
    "GARBAGE_EXTRA_WORDS": (int, 0, 100),
    "LOW_CONFIDENCE_MAX_WORDS": (int, 0, 10),
    "LOW_CONFIDENCE_LOGPROB": (float, -5.0, 0.0),
    "MIN_RMS": (float, 0.0, 0.2),
    "LIVE_MIN_RMS": (float, 0.0, 0.2),
    "WHISPER_RETRY_WITHOUT_PROMPT": (bool, None, None),
    "WHISPER_PROMPT_DAILY_CODE": (bool, None, None),
    "WHISPER_COMPRESSION_LIMIT": (float, 0.0, 50.0),
    "WHISPER_NO_SPEECH_PROB": (float, 0.0, 1.0),
    "WHISPER_NO_SPEECH_LOGPROB": (float, -5.0, 0.0),
    "DICTIONARY_ECHO_MIN_RUN": (int, 0, 20),
    "REJECT_BARE_CALLSIGN": (bool, None, None),
    "PHRASE_FRESH_S": (int, 30, 3600),
    "SPECTRAL_NOTIFICATION_MODE": (str, ("off", "shadow", "active"), None),
    "SPECTRAL_NOTIFICATION_THRESHOLD": (float, 0.3, 0.99),
    "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S": (float, 0.0, 5.0),
    "SPECTRAL_NOTIFICATION_MAX_EXTRA": (float, 0.0, 1.0),
}


class RuntimeSettingsConflict(Exception):
    pass


def defaults():
    import config
    return {key: kind(getattr(config, key)) for key, (kind, _, _) in FIELDS.items()}


def validate(values):
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValueError("Невідомі налаштування")
    clean = {}
    for key, value in values.items():
        kind, low, high = FIELDS[key]
        if kind is str:
            if value not in low:
                raise ValueError(f"{key}: допустимо {', '.join(low)}")
            clean[key] = value
            continue
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
            "limits": {k: ({"type": "str", "choices": list(low)} if kind is str else
                           {"min": low, "max": high, "type": kind.__name__})
                       for k, (kind, low, high) in FIELDS.items()}}


def write_runtime_settings(db_path, values, expected_revision):
    clean = validate(values)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _ensure(db)
        current, revision = _read(db)
        if revision != expected_revision:
            raise RuntimeSettingsConflict("Налаштування змінено в іншому вікні. Онови сторінку.")
        current.update(clean)
        # Лише відмінне від config.py: інакше зміна config.py вже ні на що б не впливала.
        base = defaults()
        changed = {key: value for key, value in current.items() if value != base[key]}
        db.execute("""INSERT INTO runtime_settings (id, values_json, revision) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET values_json = excluded.values_json, revision = excluded.revision,
            updated_at = CURRENT_TIMESTAMP""", (json.dumps(changed), revision + 1))
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
            from shared.diagnostics import runtime
            runtime.gauge("runtime_settings.revision", revision)
        except Exception:
            pass
        if self.log is not None and changed:
            self.log.info("Налаштування обробки оновлено без перезапуску (ревізія %d)", revision)
        return True
