"""/start, заявки на доступ, рішення адміністратора й особисті режими в Telegram."""

import html
import json

import requests

from services.access_requests import (
    mark_undelivered, profile_of, register_request, request_token, resolve_request,
)
from services.telegram_common import (
    ACCESS_CALLBACK_RE, MODE_COMMANDS, MODE_HELP, MODE_NAMES, ROLE_LABELS,
)
from services.user_notify_store import apply_telegram_user_mode, save_user_mode


class AccessMixin:
    def _mode_hint(self, user_id, role):
        state = self._user_state(user_id)
        current = f"Ваш режим сповіщень: {MODE_NAMES[state[1]]}. " if state else ""
        return f"{current}Змінити: {MODE_HELP.get(role, MODE_HELP['viewer'])}."

    # ---------- /start ----------

    def _handle_start(self, msg):
        """Обробляє /start в особистому чаті. Повертає True, якщо це був /start."""
        words = (msg.get("text") or "").split()
        if not words or words[0].split("@", 1)[0].lower() != "/start":
            return False
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or type(sender.get("id")) is not int or sender["id"] != chat.get("id"):
            return False

        user_chat = chat["id"]
        state, role = register_request(self.db_path, sender)
        if state == "member":
            extra = "\nЗапити на доступ нових користувачів надходитимуть сюди." if role == "admin" else ""
            self.send_text(
                f"✅ У вас є доступ до Radio 345 (роль: {ROLE_LABELS.get(role, role)}).\n"
                + self._mode_hint(user_chat, role) + extra,
                reply_markup=self._miniapp_markup(), chat_id=user_chat,
            )
        elif state == "new":
            if self._notify_access_request(sender):
                self.send_text(
                    "📨 Запит на доступ надіслано адміністратору. "
                    "Щойно його розглянуть, ви отримаєте повідомлення.",
                    chat_id=user_chat,
                )
            else:
                # Картку не доставлено: наступний /start надішле її знову, без доби очікування.
                mark_undelivered(self.db_path, sender["id"])
                state = "undelivered"
                self.send_text(
                    "⚠️ Не вдалося передати запит адміністратору. Спробуйте /start трохи пізніше.",
                    chat_id=user_chat,
                )
        elif state == "pending":
            self.send_text("⏳ Запит уже надіслано. Очікуйте рішення адміністратора.", chat_id=user_chat)
        elif state == "rejected":
            self.send_text("Адміністратор поки не надав доступ.", chat_id=user_chat)
        else:
            self.log.warning("Забагато нерозглянутих запитів на доступ; новий запит не надіслано")
            self.send_text("Зараз забагато запитів. Спробуйте пізніше.", chat_id=user_chat)
        self.log.info("Запит доступу через /start: %s", state)
        return True

    # ---------- Картки заявок ----------

    @staticmethod
    def _access_card(user_id, name, username):
        lines = ["👤 <b>Запит на доступ до Radio 345</b>"]
        lines.append(f"Ім’я: {html.escape(name) if name else '—'}")
        if username:
            lines.append(f"Username: @{html.escape(username)}")
        lines.append(f"Telegram ID: <code>{user_id}</code>")
        return "\n".join(lines)

    def _notify_access_request(self, sender):
        """Картка всім активним адміністраторам. True — доставлено хоча б одному."""
        name, username = profile_of(sender)
        user_id = sender["id"]
        suffix = f"{user_id}:{request_token(self.db_path, user_id) or ''}"
        markup = json.dumps({"inline_keyboard": [
            [
                {"text": "👁 Спостерігач", "callback_data": f"acc:viewer:{suffix}"},
                {"text": "✏️ Редактор", "callback_data": f"acc:editor:{suffix}"},
            ],
            [{"text": "✖️ Відхилити", "callback_data": f"acc:reject:{suffix}"}],
        ]})
        card = self._access_card(user_id, name, username)
        delivered = False
        for admin_chat in self._admin_chats():
            delivered = self.send_text(card, reply_markup=markup, chat_id=admin_chat) or delivered
        return delivered

    def _handle_access_decision(self, cq, chat_id, msg_id, data):
        match = ACCESS_CALLBACK_RE.fullmatch(data)
        actor = (cq.get("from") or {}).get("id")
        if not match or type(actor) is not int:
            self._answer_callback(cq, "Некоректний запит")
            return
        action, user_id, token = match.group(1), int(match.group(2)), match.group(3) or ""
        try:
            result = resolve_request(
                self.db_path, actor, user_id, None if action == "reject" else action, token=token,
            )
        except PermissionError:
            self._answer_callback(cq, "Лише адміністратор може надавати доступ")
            return

        status, role = result["status"], ROLE_LABELS.get(result["role"], result["role"])
        outcome = {
            "granted": f"✅ Доступ надано: {role}",
            "already": f"ℹ️ Доступ уже є: {role}",
            "admin": "ℹ️ Це адміністратор; роль змінюється лише в Mini App",
            "rejected": "✖️ Запит відхилено",
            "stale": "ℹ️ Запит уже розглянуто або він застарів; змін не внесено",
        }[status]
        card = self._access_card(user_id, result["telegram_name"], result["username"])
        try:
            # Без reply_markup Telegram прибирає кнопки: рішення не натиснуть удруге.
            self._request("post", "editMessageText", data={
                "chat_id": chat_id, "message_id": msg_id,
                "text": f"{card}\n\n<b>{outcome}</b>", "parse_mode": "HTML",
            }, timeout=15)
        except requests.RequestException as exc:
            self.log.warning("Не вдалося оновити картку запиту доступу: %s", type(exc).__name__)
        self._answer_callback(cq, outcome.split(" ", 1)[1])

        if status == "granted":
            self.send_text(
                f"✅ Адміністратор надав вам доступ до Radio 345 (роль: {role}).\n"
                "Голосові повідомлення ефіру надходитимуть у цей чат.\n"
                + self._mode_hint(user_id, result["role"]),
                reply_markup=self._miniapp_markup(), chat_id=user_id,
            )
        elif status == "rejected":
            self.send_text("Адміністратор не надав доступ до Radio 345.", chat_id=user_id)
        self.log.info("Рішення щодо запиту доступу: %s", status)

    # ---------- Особисті режими ----------

    def _handle_user_mode_command(self, msg, update_id):
        """/all, /watch, /commands, /off активного користувача. True — повідомлення оброблено."""
        text = (msg.get("text") or "").strip().lower()
        if text not in MODE_COMMANDS:
            return False
        user_id = self._private_sender(msg, msg.get("chat") or {})
        if user_id is None:
            return True
        state = self._user_state(user_id)
        if state is None:
            # Сторонніх і відключених ігноруємо мовчки; доступ просять через /start.
            return True

        mode, response = MODE_COMMANDS[text]
        try:
            if type(update_id) is int:
                applied = apply_telegram_user_mode(self.db_path, user_id, update_id, mode)
            else:
                save_user_mode(self.db_path, user_id, mode)
                applied = True
        except PermissionError:
            self.send_text(
                "⛔️ Режим /all доступний лише адміністратору. "
                f"Доступні режими: {MODE_HELP.get(state[0], MODE_HELP['viewer'])}.",
                chat_id=user_id,
            )
            return True
        except LookupError:
            # Користувача відключили між перевіркою й записом.
            return True
        if not applied:
            self.log.info("Повторну Telegram-команду %s пропущено", update_id)
            return True
        self.log.info("Особистий режим користувача змінено на: %s", mode)
        self.send_text(response, chat_id=user_id)
        return True
