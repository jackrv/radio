"""Names are display metadata; Telegram ID remains the authorization identity."""

from services.sqlite_connection import get_db


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_user_profiles (
        user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
        telegram_name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT ''
    )""")


def clean_name(value, limit=80):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("Имя должно быть строкой до 80 символов без переносов")
    return value.strip()


def remember_verified_profile(db_path, user):
    """Only call with verified initData, after checking account access."""
    def bounded(value, limit):
        return " ".join(value.split())[:limit] if isinstance(value, str) else ""

    name = " ".join(filter(None, [bounded(user.get("first_name"), 100), bounded(user.get("last_name"), 100)]))
    username = bounded(user.get("username"), 64)
    # Tests, older clients and some Telegram contexts may omit profile fields.
    if not name and not username:
        return
    with get_db(db_path) as db:
        ensure_table(db)
        current = db.execute("SELECT telegram_name, username FROM telegram_user_profiles WHERE user_id=?", (user["id"],)).fetchone()
        if current != (name, username):
            db.execute("""INSERT INTO telegram_user_profiles (user_id,telegram_name,username) VALUES (?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET telegram_name=excluded.telegram_name,username=excluded.username
            """, (user["id"], name, username))
