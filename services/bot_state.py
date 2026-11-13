import threading

from services.notify_mode_store import (
    VALID_MODES,
    load_notify_mode,
    save_notify_mode,
)


class BotState:
    """Общее состояние Telegram и режимов уведомлений."""

    def __init__(self):
        self.lock = threading.Lock()
        self.notify_mode = "all"
        self.waiting_for_correction = {}
        self._notify_mode_db_path = None

    def get_notify_mode(self):
        """Вызывается под self.lock при конкурентном доступе."""
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
                f"Недопустимый режим уведомлений: {mode!r}"
            )

        # При ошибке SQLite значение в памяти не меняется.
        if self._notify_mode_db_path is not None:
            save_notify_mode(
                self._notify_mode_db_path,
                mode,
            )

        self.notify_mode = mode
