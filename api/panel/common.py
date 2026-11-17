"""Спільні дані панелі: стан, діагностика, події за годинами."""
import hashlib
import hmac
import json
import math
import time
from pathlib import Path
import re

import config
from services.sqlite_connection import get_db
from services.db_backup import auto_backups
from services.user_notify_store import effective_mode

ROOT = Path(__file__).resolve().parents[2]


def spectral_settings():
    return {"mode": getattr(config, "SPECTRAL_NOTIFICATION_MODE", "off"),
            "threshold": float(getattr(config, "SPECTRAL_NOTIFICATION_THRESHOLD", 0.85)),
            "max_outside_s": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S", 0.2)),
            "max_extra": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_EXTRA", 0.02))}


def session_value(capability):
    """Сесія виводиться з постійного ключа: закладка працює й після перезапуску панелі."""
    return hmac.new(capability.encode(), b"radio345-local-session", hashlib.sha256).hexdigest()


def events_by_hour(db_path, hours=24, now=None):
    """Кількість подій за кожну з останніх ``hours`` годин (найстаріша — перша)."""
    now = time.time() if now is None else now
    start = int(now // 3600 - hours + 1) * 3600
    counts = [0] * hours
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT CAST(strftime('%s', ts) AS INTEGER) FROM radio_events WHERE ts >= datetime(?, 'unixepoch')",
            (start,),
        ).fetchall()
    for (stamp,) in rows:
        if stamp is not None and stamp >= start:
            index = int((stamp - start) // 3600)
            if 0 <= index < hours:
                counts[index] += 1
    return {"start": start, "counts": counts}


def users_overview(db_path):
    with get_db(db_path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "telegram_users" not in tables:
            return []
        profiles = "telegram_user_profiles" in tables
        settings = "telegram_user_settings" in tables
        rows = db.execute(f"""SELECT u.user_id, u.role, u.enabled,
                {"COALESCE(NULLIF(p.display_name,''), NULLIF(p.telegram_name,''), '')" if profiles else "''"},
                {"COALESCE(p.username,'')" if profiles else "''"},
                {"s.notify_mode" if settings else "NULL"}
            FROM telegram_users u
            {"LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id" if profiles else ""}
            {"LEFT JOIN telegram_user_settings s ON s.user_id=u.user_id" if settings else ""}
            ORDER BY u.role='admin' DESC, u.role='editor' DESC, u.user_id""").fetchall()
    return [{"user_id": uid, "role": role, "enabled": bool(enabled), "name": name, "username": username,
             "mode": effective_mode(role, mode) if enabled else "off"}
            for uid, role, enabled, name, username, mode in rows]


def backups_overview(directory):
    items = []
    for path in auto_backups(directory):
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size, "created": stat.st_mtime})
    return items


def read_diagnostics(path):
    path = Path(path)
    if not path.is_file():
        return {"available":False}
    if path.stat().st_size > 2_000_000:
        raise ValueError("Файл діагностики завеликий")
    data = json.loads(path.read_text())
    return diagnostics_view(data)


def diagnostics_view(data):
    if not isinstance(data, dict):
        raise ValueError("Некоректний знімок діагностики")
    # Telemetry only; ignore any future unrelated root fields and arbitrary strings.
    def numbers(value):
        if value is None or type(value) is bool: return value
        if type(value) in (int,float): return value if math.isfinite(value) else None
        if isinstance(value,list): return [numbers(x) for x in value[:128]]
        if isinstance(value,dict): return {k:numbers(v) for k,v in value.items() if isinstance(k,str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,90}",k)}
        if value in ("ready","ready_fallback","notification","low_rms","empty_text","garbage","blacklist","low_confidence","dictionary_echo","stale","error","whisper_error","discarded","adc","callback","s","ratio",
                     "ok","stuck","failing","loading","failed","disabled"): return value
        return None
    result = {"available":True, **{k:numbers(data[k]) for k in ("schema","started_at","updated_at","ended_at","counters","gauges","timings","asr_latency","notification_spectral") if k in data}}
    if isinstance(data.get("session"), str) and re.fullmatch(r"[0-9a-f]{32}", data["session"]):
        result["session"] = data["session"]
    if data.get("session_status") in ("running", "finished"):
        result["session_status"] = data["session_status"]
    return result
