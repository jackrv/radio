"""Постоянные настройки уведомлений Telegram."""

import logging

from services.sqlite_connection import get_db

VALID_MODES = frozenset({"all", "watch", "commands"})
DEFAULT_MODE = "all"


def ensure_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)


def load_notify_mode(db_path):
    with get_db(db_path) as db:
        ensure_table(db)

        row = db.execute(
            """
            SELECT setting_value
            FROM bot_settings
            WHERE setting_key = ?
            """,
            ("notify_mode",),
        ).fetchone()

    if row is None:
        return DEFAULT_MODE

    mode = row[0]

    if mode not in VALID_MODES:
        logging.getLogger("radio").warning(
            "Недопустимый сохранённый режим Telegram: %r. "
            "Используется ALL.",
            mode,
        )
        return DEFAULT_MODE

    return mode


def save_notify_mode(db_path, mode):
    if mode not in VALID_MODES:
        raise ValueError(
            f"Недопустимый режим Telegram: {mode!r}"
        )

    with get_db(db_path) as db:
        ensure_table(db)

        db.execute(
            """
            INSERT INTO bot_settings (
                setting_key, setting_value
            )
            VALUES (?, ?)
            ON CONFLICT(setting_key)
            DO UPDATE SET
                setting_value = excluded.setting_value
            """,
            ("notify_mode", mode),
        )
