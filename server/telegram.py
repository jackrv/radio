"""Telegram-бот Radio 345.

Сервіс зібрано з частин за відповідальністю:

- ``telegram_common``  — константи, HTTP, текст, ролі з бази, службові повідомлення;
- ``telegram_outbox``  — розсилка голосових за особистими режимами, черга;
- ``telegram_access``  — /start, заявки на доступ, рішення, особисті режими;
- ``telegram_updates`` — розбір оновлень, кнопки повідомлень, long polling.
- ``telegram_wishlist`` — додавання приватних побажань адміністратора у .wishlist.

Права й отримувачів визначає лише база користувачів.
"""

from shared.telegram_maintenance import MaintenanceMixin
from server.telegram_access import AccessMixin
from shared.telegram_common import TelegramBase
from shared.telegram_outbox import OutboxMixin
from server.telegram_updates import UpdatesMixin
from server.telegram_wishlist import WishlistMixin


class TelegramService(MaintenanceMixin, UpdatesMixin, AccessMixin, WishlistMixin, OutboxMixin, TelegramBase):
    def __init__(self, bot_token, sample_rate, db_path, log):
        super().__init__(bot_token, sample_rate, db_path, log)
        self._init_outbox()
        self._init_maintenance()
