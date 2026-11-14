"""Особисті режими сповіщень користувачів Telegram.

Кожен активний користувач отримує голосові повідомлення у свій особистий
чат за власним режимом. Режим ``all`` доступний лише ролі ``admin``.
"""

from contextlib import nullcontext

from services.sqlite_connection import get_db
from services.user_store import _valid_id

VALID_MODES = frozenset({"all", "watch", "commands", "off"})
# Режим без збереженого вибору: адміністратор — усі фрази, решта — команди й позивні.
DEFAULT_ADMIN_MODE = "all"
DEFAULT_USER_MODE = "commands"
ADMIN_ONLY_MODES = frozenset({"all"})


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_user_settings (
        user_id INTEGER PRIMARY KEY,
        notify_mode TEXT NOT NULL CHECK (notify_mode IN ('all','watch','commands','off'))
    )""")


def allowed_modes(role):
    return sorted(VALID_MODES if role == "admin" else VALID_MODES - ADMIN_ONLY_MODES)


def effective_mode(role, stored):
    mode = stored or (DEFAULT_ADMIN_MODE if role == "admin" else DEFAULT_USER_MODE)
    if mode in ADMIN_ONLY_MODES and role != "admin":
        # Після пониження ролі «all» більше не діє.
        return DEFAULT_USER_MODE
    return mode


def _users_exist(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_users'"
    ).fetchone() is not None


def list_recipients(db_path, *, db=None):
    """Активні користувачі: ``[{"chat_id", "role", "mode"}]``."""
    with (get_db(db_path) if db is None else nullcontext(db)) as conn:
        if not _users_exist(conn):
            return []
        ensure_table(conn)
        rows = conn.execute("""SELECT u.user_id, u.role, s.notify_mode
            FROM telegram_users u LEFT JOIN telegram_user_settings s ON s.user_id = u.user_id
            WHERE u.enabled = 1 ORDER BY u.user_id""").fetchall()
    return [
        {"chat_id": user_id, "role": role, "mode": effective_mode(role, stored)}
        for user_id, role, stored in rows
    ]


def load_user_mode(db_path, user_id):
    """``(роль, режим)`` активного користувача або None."""
    _valid_id(user_id)
    with get_db(db_path) as db:
        if not _users_exist(db):
            return None
        ensure_table(db)
        row = db.execute("""SELECT u.role, s.notify_mode FROM telegram_users u
            LEFT JOIN telegram_user_settings s ON s.user_id = u.user_id
            WHERE u.user_id = ? AND u.enabled = 1""", (user_id,)).fetchone()
        if row is None:
            return None
        return row[0], effective_mode(row[0], row[1])


def _store_mode(db, user_id, mode):
    if mode not in VALID_MODES:
        raise ValueError("Неприпустимий режим сповіщень")
    row = db.execute(
        "SELECT role FROM telegram_users WHERE user_id = ? AND enabled = 1", (user_id,),
    ).fetchone() if _users_exist(db) else None
    if row is None:
        raise LookupError("Користувача не знайдено або доступ вимкнено")
    if mode not in allowed_modes(row[0]):
        raise PermissionError("Режим «Усі повідомлення» доступний лише адміністратору")
    ensure_table(db)
    db.execute("""INSERT INTO telegram_user_settings (user_id, notify_mode) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET notify_mode = excluded.notify_mode""", (user_id, mode))


def save_user_mode(db_path, user_id, mode):
    _valid_id(user_id)
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _store_mode(db, user_id, mode)


def apply_telegram_user_mode(db_path, user_id, update_id, mode):
    """Атомарно зберігає команду режиму з Telegram разом з offset.

    True — застосовано вперше; False — це оновлення вже підтверджене.
    PermissionError/LookupError не зсувають offset: повтор отримає ту саму відповідь.
    """
    from services.telegram_offset_store import _ensure_table as ensure_offset_table

    _valid_id(user_id)
    if type(update_id) is not int or update_id < 0:
        raise ValueError("Некоректний Telegram update_id")
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_offset_table(db)
        row = db.execute("SELECT next_offset FROM telegram_poll_state WHERE id = 1").fetchone()
        if row is not None and update_id < row[0]:
            return False
        _store_mode(db, user_id, mode)
        db.execute("""INSERT INTO telegram_poll_state (id, next_offset) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET next_offset = MAX(telegram_poll_state.next_offset, excluded.next_offset)
        """, (update_id + 1,))
    return True
