"""Запити на доступ через /start у Telegram і рішення адміністратора.

Telegram ID — ідентичність для авторизації; ім’я та username лише
відображаються. Таблиця запитів обмежує повторні сповіщення адміністратора.
"""

import time

from services.sqlite_connection import get_db
from services.user_profiles import ensure_table as ensure_profiles
from services.user_store import _require_permission, _valid_id, init_users_db

# Повторне сповіщення про той самий запит або новий запит після відмови.
REQUEST_RETRY_SECONDS = 24 * 3600
# Скільки нерозглянутих запитів одночасно може надійти адміністратору.
MAX_PENDING_REQUESTS = 20
GRANTABLE_ROLES = ("viewer", "editor")


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_access_requests (
        user_id INTEGER PRIMARY KEY,
        telegram_name TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('pending', 'rejected')),
        requested_at REAL NOT NULL
    )""")


def _bounded(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def profile_of(sender):
    """Ім’я та username із поля from повідомлення Telegram."""
    name = " ".join(filter(None, [
        _bounded(sender.get("first_name"), 100),
        _bounded(sender.get("last_name"), 100),
    ]))
    return name, _bounded(sender.get("username"), 64)


def register_request(db_path, sender, now=None):
    """Реєструє /start від користувача.

    Повертає ``(стан, роль)``:
    ``member`` — доступ уже є; ``new`` — треба сповістити адміністратора;
    ``pending`` — запит уже надіслано; ``rejected`` — нещодавно відхилено;
    ``limit`` — забагато нерозглянутих запитів.
    """
    user_id = _valid_id(sender.get("id"))
    now = time.time() if now is None else now
    name, username = profile_of(sender)
    init_users_db(db_path)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        member = db.execute(
            "SELECT role FROM telegram_users WHERE user_id=? AND enabled=1", (user_id,),
        ).fetchone()
        if member:
            db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
            return "member", member[0]

        current = db.execute(
            "SELECT status, requested_at FROM telegram_access_requests WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if current and now - current[1] < REQUEST_RETRY_SECONDS:
            return current[0], None

        pending = db.execute(
            "SELECT COUNT(*) FROM telegram_access_requests WHERE status='pending' AND requested_at>?",
            (now - REQUEST_RETRY_SECONDS,),
        ).fetchone()[0]
        if pending >= MAX_PENDING_REQUESTS:
            return "limit", None

        db.execute("""INSERT INTO telegram_access_requests
            (user_id, telegram_name, username, status, requested_at) VALUES (?,?,?,'pending',?)
            ON CONFLICT(user_id) DO UPDATE SET telegram_name=excluded.telegram_name,
                username=excluded.username, status='pending', requested_at=excluded.requested_at
        """, (user_id, name, username, now))
        return "new", None


def resolve_request(db_path, actor_id, user_id, role, now=None):
    """Рішення адміністратора: ``role`` — viewer/editor або None для відмови.

    Повертає словник зі ``status``: ``granted``, ``rejected``, ``already``
    (доступ із цією роллю вже є) або ``admin`` (користувач — адміністратор;
    змінювати його тут не можна).
    """
    _valid_id(user_id)
    if role is not None and role not in GRANTABLE_ROLES:
        raise ValueError("Роль неприпустима")
    now = time.time() if now is None else now
    init_users_db(db_path)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        ensure_profiles(db)
        _require_permission(db, actor_id, "users.manage")
        request = db.execute(
            "SELECT telegram_name, username FROM telegram_access_requests WHERE user_id=?",
            (user_id,),
        ).fetchone()
        name, username = request or ("", "")
        result = {"user_id": user_id, "telegram_name": name, "username": username, "role": role}
        user = db.execute(
            "SELECT role, enabled FROM telegram_users WHERE user_id=?", (user_id,),
        ).fetchone()

        if user and user[0] == "admin":
            db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
            return {**result, "status": "admin", "role": "admin"}

        if role is None:
            if user and user[1] == 1:
                return {**result, "status": "already", "role": user[0]}
            db.execute(
                "UPDATE telegram_access_requests SET status='rejected', requested_at=? WHERE user_id=?",
                (now, user_id),
            )
            return {**result, "status": "rejected"}

        if user == (role, 1):
            db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
            return {**result, "status": "already"}

        db.execute("""INSERT INTO telegram_users (user_id, role, enabled) VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, enabled=1
        """, (user_id, role))
        if name or username:
            db.execute("""INSERT INTO telegram_user_profiles (user_id, telegram_name, username)
                VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                telegram_name=excluded.telegram_name, username=excluded.username
            """, (user_id, name, username))
        db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
        return {**result, "status": "granted"}
