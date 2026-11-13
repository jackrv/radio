import threading


class BotState:
    """Общее состояние Telegram и режимов уведомлений."""

    def __init__(self):
        self.lock = threading.Lock()
        self.notify_mode = "all"
        self.waiting_for_correction = {}

    def get_notify_mode(self):
        """Вызывается под self.lock при конкурентном доступе."""
        return self.notify_mode

    def set_notify_mode(self, mode):
        """Вызывается под self.lock при конкурентном доступе."""
        self.notify_mode = mode
