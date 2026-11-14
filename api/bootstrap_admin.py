"""Первоначальная локальная настройка администратора.

Те саме робить start_radio345.py, якщо в базі немає адміністратора.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

import config
from services.admin_setup import ensure_admin
from services.dictionary_setup import has_active_admin, interactive_console


def main():
    load_dotenv(config.BASE_DIR / ".env")

    db_path = Path(config.DB_PATH)
    if not db_path.is_file():
        raise SystemExit(
            "Робочу SQLite не знайдено. Спочатку імпортуйте словники: "
            "start_radio345.py або manage_database.py init"
        )
    if has_active_admin(db_path):
        print("Адміністратора вже зареєстровано.")
        return

    if not ensure_admin(
        db_path,
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "python -m api.bootstrap_admin",
        interactive=interactive_console(),
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
