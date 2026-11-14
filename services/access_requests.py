"""Запити на доступ через /start у Telegram і рішення адміністратора.

Telegram ID — ідентичність для авторизації; ім’я та username лише
відображаються. Таблиця запитів обмежує повторні сповіщення адміністратора.
"""

import secrets
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
        requested_at REAL NOT NULL,
        token TEXT NOT NULL DEFAULT ''
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_access_requests)")}
    if "token" not in columns:
        db.execute("ALTER TABLE telegram_access_requests ADD COLUMN token TEXT NOT NULL DEFAULT ''")


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

        # Новий токен — нова заявка: кнопки старих карток до неї не застосовні.
        db.execute("""INSERT INTO telegram_access_requests
            (user_id, telegram_name, username, status, requested_at, token) VALUES (?,?,?,'pending',?,?)
            ON CONFLICT(user_id) DO UPDATE SET telegram_name=excluded.telegram_name,
                username=excluded.username, status='pending', requested_at=excluded.requested_at,
                token=excluded.token
        """, (user_id, name, username, now, secrets.token_hex(4)))
        return "new", None


def request_token(db_path, user_id):
    """Токен заявки, що очікує рішення, для кнопок картки (або None)."""
    with get_db(db_path) as db:
        ensure_table(db)
        row = db.execute(
            "SELECT token FROM telegram_access_requests WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
    return row[0] if row and row[0] else None


def resolve_request(db_path, actor_id, user_id, role, now=None, token=None):
    """Рішення адміністратора: ``role`` — viewer/editor або None для відмови.

    Повертає словник зі ``status``: ``granted``, ``rejected``, ``already``
    (доступ із цією роллю вже є), ``admin`` (користувач — адміністратор;
    змінювати його тут не можна) або ``stale`` (заявки, що очікує рішення,
    немає або ``token`` не збігається: кнопка старої картки не повинна
    спрацювати на нову заявку чи скасувати пізніше відключення).
    Кнопки Telegram завжди передають ``token``; None — виклик без картки.
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
            "SELECT telegram_name, username, status, token FROM telegram_access_requests WHERE user_id=?",
            (user_id,),
        ).fetchone()
        name, username, request_status, request_token_value = request or ("", "", None, "")
        result = {"user_id": user_id, "telegram_name": name, "username": username, "role": role}
        user = db.execute(
            "SELECT role, enabled FROM telegram_users WHERE user_id=?", (user_id,),
        ).fetchone()

        if user and user[0] == "admin":
            db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
            return {**result, "status": "admin", "role": "admin"}

        if user and user[1] == 1 and (role is None or user[0] == role):
            if request_status == "pending":
                db.execute("DELETE FROM telegram_access_requests WHERE user_id=?", (user_id,))
            return {**result, "status": "already", "role": user[0]}

        if request_status != "pending" or (token is not None and token != request_token_value):
            return {**result, "status": "stale"}

        if role is None:
            db.execute(
                "UPDATE telegram_access_requests SET status='rejected', requested_at=? WHERE user_id=?",
                (now, user_id),
            )
            return {**result, "status": "rejected"}

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


def mark_undelivered(db_path, user_id):
    """Карточку заявки не доставлено: наступний /start надішле її знову."""
    with get_db(db_path) as db:
        ensure_table(db)
        db.execute(
            "UPDATE telegram_access_requests SET requested_at = 0 "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
