"""Постоянные настройки уведомлений Telegram."""

import logging

from services.sqlite_connection import get_db

VALID_MODES = frozenset({"all", "watch", "commands", "off"})
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
            "Неприпустимий збережений режим Telegram: %r. "
            "Використовується ALL.",
            mode,
        )
        return DEFAULT_MODE

    return mode


def save_notify_mode(db_path, mode):
    if mode not in VALID_MODES:
        raise ValueError(
            f"Неприпустимий режим Telegram: {mode!r}"
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


def apply_telegram_notify_mode(db_path, update_id, mode):
    """Атомарно сохраняет команду Telegram и её offset.

    Возвращает True, если команда применена впервые.
    False означает, что это обновление уже подтверждено.
    """
    from services.telegram_offset_store import (
        _ensure_table as ensure_offset_table,
    )

    if mode not in VALID_MODES:
        raise ValueError("Неприпустимий режим сповіщень")

    if type(update_id) is not int or update_id < 0:
        raise ValueError("Некоректний Telegram update_id")

    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_table(db)
        ensure_offset_table(db)

        row = db.execute("""
            SELECT next_offset
            FROM telegram_poll_state
            WHERE id = 1
        """).fetchone()

        if row is not None and update_id < row[0]:
            return False

        db.execute("""
            INSERT INTO bot_settings
                (setting_key, setting_value)
            VALUES ('notify_mode', ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value
        """, (mode,))

        db.execute("""
            INSERT INTO telegram_poll_state
                (id, next_offset)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                next_offset = MAX(
                    telegram_poll_state.next_offset,
                    excluded.next_offset
                )
        """, (update_id + 1,))

    return True
