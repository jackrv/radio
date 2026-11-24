"""Захист робочої бази: тести працюють лише з тимчасовими SQLite-файлами."""

import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATABASES = {_ROOT / "events.db", _ROOT / ".local-data" / "events.db",
                  _ROOT / ".local-recordings" / "index.sqlite3", _ROOT / ".local-data" / "recordings" / "index.sqlite3"}
_connect = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    target = str(database)
    if target.startswith("file:"):
        target = target[5:].split("?", 1)[0]
    if target and target != ":memory:" and Path(target).resolve() in LIVE_DATABASES:
        raise RuntimeError("Тести не можуть відкривати робочу events.db — використовуйте tmp_path")
    return _connect(database, *args, **kwargs)


sqlite3.connect = _guarded_connect
