"""Пользователи Telegram Mini App и серверные права доступа."""

from services.sqlite_connection import get_db

PERMISSIONS = {
    "viewer": frozenset({
        "status.read", "events.read", "dictionaries.read",
    }),
    "editor": frozenset({
        "status.read", "events.read", "dictionaries.read",
        "corrections.write", "dictionaries.write",
    }),
    "admin": frozenset({
        "status.read", "events.read", "dictionaries.read",
        "corrections.write", "dictionaries.write",
        "settings.read", "settings.write",
        "audio.control", "users.manage",
    }),
}


def _valid_id(user_id):
    if type(user_id) is not int or not (0 < user_id < 2**63):
        raise ValueError("Некоректний Telegram user_id")
    return user_id


def init_users_db(db_path):
    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL
                    CHECK (role IN ('admin','editor','viewer')),
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (enabled IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


def bootstrap_admin(db_path, user_id):
    """Однократная локальная инициализация. Не вызывать через API."""
    _valid_id(user_id)

    with get_db(db_path) as db:
        current = db.execute(
            "SELECT role, enabled FROM telegram_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if current == ("admin", 1):
            return False

        if db.execute(
            "SELECT 1 FROM telegram_users LIMIT 1"
        ).fetchone():
            raise RuntimeError(
                "Користувачі вже є: bootstrap заборонено"
            )

        db.execute(
            "INSERT INTO telegram_users (user_id,role) "
            "VALUES (?,'admin')",
            (user_id,),
        )

        return True


def _require_permission(db, user_id, permission):
    _valid_id(user_id)

    if permission not in PERMISSIONS["admin"]:
        raise ValueError("Невідомий дозвіл")

    row = db.execute(
        "SELECT role, enabled FROM telegram_users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if (
        row is None
        or row[1] != 1
        or permission not in PERMISSIONS[row[0]]
    ):
        raise PermissionError("Доступ заборонено")

    return row[0]


def require_permission(db_path, user_id, permission):
    with get_db(db_path) as db:
        return _require_permission(db, user_id, permission)


def create_user(db_path, actor_id, user_id, *, role="viewer"):
    """Добавление пользователя только администратором."""
    _valid_id(user_id)

    if role not in ("viewer", "editor"):
        raise ValueError("Роль неприпустима для створення")

    with get_db(db_path) as db:
        _require_permission(db, actor_id, "users.manage")

        db.execute(
            "INSERT INTO telegram_users (user_id,role) "
            "VALUES (?,?)",
            (user_id, role),
        )
