"""Локальный сервер API Radio 345."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

import config
from api.app import create_app
from services.sqlite_connection import get_db


def main():
    load_dotenv(config.BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    db_path = Path(config.DB_PATH)

    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN відсутній у .env"
        )

    if not db_path.is_file():
        raise SystemExit("Робочу SQLite не знайдено")

    with get_db(db_path) as db:
        row = db.execute("""
            SELECT COUNT(*)
            FROM telegram_users
            WHERE role = 'admin' AND enabled = 1
        """).fetchone()

    if not row or row[0] == 0:
        raise SystemExit(
            "Немає активного адміністратора. "
            "Спочатку виконайте bootstrap_admin."
        )

    app = create_app(db_path, token)

    print("Radio 345 API")
    print("Адрес: http://127.0.0.1:8765")
    print("Доступ: лише локальний комп’ютер")

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        reload=False,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
