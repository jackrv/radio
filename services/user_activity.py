"""Журнал дій користувачів Mini App і бота — для сторінки «Активність» у локальній панелі.

Записуються лише дії, зроблені після появи журналу (16.11.2026): раніше база не
зберігала, хто саме виправив текст чи змінив налаштування. Запис у журнал ніколи
не зупиняє саму дію: помилка лише потрапляє в журнал програми.
"""

import json
import logging
import time

from services.sqlite_connection import get_db

RETENTION_S = 365 * 24 * 3600
OPEN_INTERVAL_S = 30 * 60          # «відкрито Mini App» — не частіше, ніж раз на 30 хв
MAX_TEXT = 300

log = logging.getLogger("radio")


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS user_activity (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '{}')""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_activity_user_ts ON user_activity(user_id, ts)")


def _short(value):
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def record(db_path, user_id, action, target="", details=None, *, now=None):
    """Записати дію; ніколи не кидає виняток."""
    try:
        now = time.time() if now is None else now
        details = {key: _short(value) for key, value in (details or {}).items()}
        with get_db(db_path) as db:
            ensure_table(db)
            db.execute("INSERT INTO user_activity (user_id, ts, action, target, details) VALUES (?,?,?,?,?)",
                       (int(user_id), now, action, str(target or ""), json.dumps(details, ensure_ascii=False)))
            db.execute("DELETE FROM user_activity WHERE ts < ?", (now - RETENTION_S,))
    except Exception:
        log.exception("Не вдалося записати дію користувача в журнал активності")


def touch_open(db_path, user_id, *, now=None):
    """«Відкрито Mini App»: не частіше ніж раз на OPEN_INTERVAL_S для одного користувача."""
    try:
        now = time.time() if now is None else now
        with get_db(db_path) as db:
            ensure_table(db)
            last = db.execute("SELECT MAX(ts) FROM user_activity WHERE user_id=? AND action='open'",
                              (int(user_id),)).fetchone()[0]
        if last is None or now - last >= OPEN_INTERVAL_S:
            record(db_path, user_id, "open", now=now)
    except Exception:
        log.exception("Не вдалося записати відкриття Mini App")


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def users(db_path):
    with get_db(db_path) as db:
        if not _table_exists(db, "telegram_users"):
            return []
        profiles = _table_exists(db, "telegram_user_profiles")
        rows = db.execute(
            "SELECT u.user_id, u.role, u.enabled, " +
            ("COALESCE(NULLIF(p.display_name,''), NULLIF(p.telegram_name,''), NULLIF(p.username,''), '') "
             "FROM telegram_users u LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id "
             if profiles else "'' FROM telegram_users u ") +
            "ORDER BY CASE u.role WHEN 'admin' THEN 1 ELSE 0 END, u.user_id").fetchall()
    return [{"user_id": r[0], "role": r[1], "enabled": bool(r[2]), "name": r[3]} for r in rows]


def overview(db_path, user_id, *, limit=200, now=None):
    """Статистика й останні дії одного користувача."""
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        ensure_table(db)
        user = db.execute("SELECT role, enabled, created_at FROM telegram_users WHERE user_id=?",
                          (user_id,)).fetchone() if _table_exists(db, "telegram_users") else None
        mode = db.execute("SELECT notify_mode FROM telegram_user_settings WHERE user_id=?",
                          (user_id,)).fetchone() if _table_exists(db, "telegram_user_settings") else None
        delivered = dict(db.execute("SELECT kind, COUNT(*) FROM telegram_deliveries WHERE chat_id=? GROUP BY kind",
                                    (user_id,)).fetchall()) if _table_exists(db, "telegram_deliveries") else {}
        counts = dict(db.execute("SELECT action, COUNT(*) FROM user_activity WHERE user_id=? GROUP BY action",
                                 (user_id,)).fetchall())
        week = dict(db.execute("SELECT action, COUNT(*) FROM user_activity WHERE user_id=? AND ts>=? GROUP BY action",
                               (user_id, now - 7 * 24 * 3600)).fetchall())
        first, last, days = db.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(DISTINCT date(ts,'unixepoch','localtime')) FROM user_activity WHERE user_id=?",
            (user_id,)).fetchone()
        changed = db.execute("SELECT COUNT(*) FROM user_activity WHERE user_id=? AND action='correction' "
                             "AND json_extract(details,'$.changed')=1", (user_id,)).fetchone()[0]
        rows = db.execute("SELECT ts, action, target, details FROM user_activity WHERE user_id=? "
                          "ORDER BY ts DESC, id DESC LIMIT ?", (user_id, limit)).fetchall()
    if user is None:
        raise LookupError("Користувача не знайдено")
    return {
        "user_id": user_id, "role": user[0], "enabled": bool(user[1]), "created_at": user[2],
        "notify_mode": mode[0] if mode else None,
        "delivered": {"total": sum(delivered.values()), "by_kind": delivered},
        "actions": counts, "actions_week": week, "corrections_changed": changed,
        "first_logged": first, "last_activity": last, "active_days": days,
        "items": [{"ts": ts, "action": action, "target": target, "details": json.loads(details or "{}")}
                  for ts, action, target, details in rows],
    }


def corrections(db_path, fix_ids=None, user_id=None):
    """Правки тексту з журналу: {fix_id: [{user_id, name, ts, from, to}]}, новіші — першими."""
    with get_db(db_path) as db:
        ensure_table(db)
        profiles = _table_exists(db, "telegram_user_profiles")
        sql = ("SELECT a.target, a.user_id, a.ts, a.details, " +
               ("COALESCE(NULLIF(p.display_name,''), NULLIF(p.telegram_name,''), NULLIF(p.username,''), '') "
                "FROM user_activity a LEFT JOIN telegram_user_profiles p ON p.user_id=a.user_id "
                if profiles else "'' FROM user_activity a ") +
               "WHERE a.action='correction'")
        params = []
        if user_id is not None:
            sql += " AND a.user_id=?"
            params.append(user_id)
        if fix_ids is not None:
            sql += " AND a.target IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(sorted(set(fix_ids))))
        rows = db.execute(sql + " ORDER BY a.ts DESC", params).fetchall()
    result = {}
    for target, uid, ts, details, name in rows:
        d = json.loads(details or "{}")
        result.setdefault(target, []).append({"user_id": uid, "name": name or str(uid), "ts": ts,
                                              "from": d.get("from"), "to": d.get("to"), "changed": d.get("changed")})
    return result


def editors(db_path):
    """Користувачі, чиї правки є в журналі: [{user_id, name, corrections}]."""
    people = {u["user_id"]: u for u in users(db_path)}
    with get_db(db_path) as db:
        ensure_table(db)
        rows = db.execute("SELECT user_id, COUNT(DISTINCT target) FROM user_activity WHERE action='correction' "
                          "GROUP BY user_id ORDER BY 2 DESC").fetchall()
    return [{"user_id": uid, "name": (people.get(uid) or {}).get("name") or str(uid), "corrections": n}
            for uid, n in rows]
