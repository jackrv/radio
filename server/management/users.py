"""Користувачі: перелік і зміна ролі, доступу, імені й режиму сповіщень."""
from shared.sqlite_connection import get_db
from shared.user_store import _require_permission, _valid_id
from server.user_profiles import ensure_table as ensure_profiles, clean_name
from shared.user_notify_store import ensure_table as ensure_notify, effective_mode, allowed_modes
from shared.management.common import ManagementConflict
from shared.management.common import version


def list_users(db_path, actor_id):
    with get_db(db_path) as db:
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        ensure_notify(db)
        rows = db.execute("""SELECT u.user_id, u.role, u.enabled, COALESCE(p.display_name,''),
            COALESCE(p.telegram_name,''), COALESCE(p.username,''), s.notify_mode FROM telegram_users u
            LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id
            LEFT JOIN telegram_user_settings s ON s.user_id=u.user_id ORDER BY u.user_id""").fetchall()
    return {"items": [{"user_id": str(uid), "role": role, "enabled": bool(enabled),
                       "display_name": custom, "telegram_name": name, "username": username,
                       "name": custom or name or ("@" + username if username else f"Telegram {uid}"),
                       "notify_mode": effective_mode(role, mode),
                       "version": version((uid, role, enabled, custom, mode))}
                      for uid, role, enabled, custom, name, username, mode in rows]}


def write_user(db_path, actor_id, user_id, role, enabled, expected_version, display_name=None, notify_mode=None):
    _valid_id(user_id)
    if role not in ("admin", "editor", "viewer"):
        raise ValueError("Невідома роль")
    if display_name is not None:
        display_name = clean_name(display_name)
    if notify_mode is not None and notify_mode not in allowed_modes(role):
        raise ValueError("Цей режим сповіщень недоступний для вибраної ролі")
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_permission(db, actor_id, "users.manage")
        ensure_profiles(db)
        ensure_notify(db)
        row = db.execute("SELECT user_id, role, enabled FROM telegram_users WHERE user_id = ?",
                         (user_id,)).fetchone()
        profile = db.execute("SELECT display_name FROM telegram_user_profiles WHERE user_id=?", (user_id,)).fetchone()
        mode_row = db.execute("SELECT notify_mode FROM telegram_user_settings WHERE user_id=?", (user_id,)).fetchone()
        stored_mode = mode_row[0] if mode_row else None
        if (version((*row, profile[0] if profile else "", stored_mode)) if row else None) != expected_version:
            raise ManagementConflict("Користувач змінився. Онови список.")
        if row and row[1:] == ("admin", 1) and (role != "admin" or not enabled):
            admins = db.execute("SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError("Не можна вимкнути або понизити останнього адміністратора")
        db.execute("""
            INSERT INTO telegram_users (user_id, role, enabled) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, enabled=excluded.enabled
        """, (user_id, role, int(enabled)))
        if notify_mode is not None:
            db.execute("""INSERT INTO telegram_user_settings VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET notify_mode=excluded.notify_mode""", (user_id, notify_mode))
        if display_name is not None:
            db.execute("""INSERT INTO telegram_user_profiles (user_id,display_name) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name""", (user_id, display_name))
    return {"saved": True}
