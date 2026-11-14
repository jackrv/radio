import logging
import sqlite3
import threading

from services.notify_mode_store import (
    VALID_MODES,
    load_notify_mode,
    save_notify_mode,
    apply_telegram_notify_mode,
)


class BotState:
    """Общее состояние Telegram и режимов уведомлений."""

    def __init__(self):
        self.lock = threading.Lock()
        self.notify_mode = "all"
        self.waiting_for_correction = {}
        self._notify_mode_db_path = None

    def get_notify_mode(self):
        """Возвращает актуальный режим из общей SQLite."""
        if self._notify_mode_db_path is None:
            return self.notify_mode

        try:
            self.notify_mode = load_notify_mode(
                self._notify_mode_db_path
            )
        except sqlite3.Error:
            logging.getLogger("radio").exception(
                "Не вдалося прочитати режим сповіщень. "
                "Автоматичне надсилання тимчасово вимкнено."
            )
            return "off"

        return self.notify_mode

    def enable_persistence(self, db_path):
        """Восстанавливает режим до запуска рабочих потоков."""

        restored = load_notify_mode(db_path)

        with self.lock:
            self.notify_mode = restored
            self._notify_mode_db_path = str(db_path)

    def set_notify_mode(self, mode):
        """Вызывается под self.lock."""

        if mode not in VALID_MODES:
            raise ValueError(
                f"Неприпустимий режим сповіщень: {mode!r}"
            )

        # При ошибке SQLite значение в памяти не меняется.
        if self._notify_mode_db_path is not None:
            save_notify_mode(
                self._notify_mode_db_path,
                mode,
            )

        self.notify_mode = mode


    def apply_telegram_mode(self, mode, update_id):
        """Вызывается под self.lock из TelegramService."""
        if self._notify_mode_db_path is None:
            raise RuntimeError(
                "Сначала включите сохранение BotState"
            )

        applied = apply_telegram_notify_mode(
            self._notify_mode_db_path,
            update_id,
            mode,
        )

        if applied:
            self.notify_mode = mode
        else:
            self.notify_mode = load_notify_mode(
                self._notify_mode_db_path
            )

        return applied
