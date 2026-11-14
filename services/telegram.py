"""Telegram-бот Radio 345.

Сервіс зібрано з частин за відповідальністю:

- ``telegram_common``  — константи, HTTP, текст, ролі з бази, службові повідомлення;
- ``telegram_outbox``  — розсилка голосових за особистими режимами, черга;
- ``telegram_access``  — /start, заявки на доступ, рішення, особисті режими;
- ``telegram_updates`` — розбір оновлень, кнопки повідомлень, long polling.

Права й отримувачів визначає лише база користувачів.
"""

from services.telegram_access import AccessMixin
from services.telegram_common import TelegramBase
from services.telegram_outbox import OutboxMixin
from services.telegram_updates import UpdatesMixin


class TelegramService(UpdatesMixin, AccessMixin, OutboxMixin, TelegramBase):
    def __init__(self, bot_token, sample_rate, db_path, log):
        super().__init__(bot_token, sample_rate, db_path, log)
        self._init_outbox()
