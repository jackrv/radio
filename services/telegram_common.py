"""Спільна основа Telegram-бота: константи, HTTP-запити, текст і ролі.

Права й отримувачів визначає лише база користувачів. TELEGRAM_CHAT_ID
потрібен тільки для реєстрації першого адміністратора (api.bootstrap_admin).
"""

import json
import logging
import re
import sqlite3
import threading
import time
from urllib.parse import quote

import requests

from core.diagnostics import error_kind, runtime
from services.telegram_media_store import FIX_RE
from services.user_notify_store import list_recipients, load_user_mode

MINIAPP_URL = "https://jackrv.github.io/radio/"
OGG_TIMEOUT_S = 30
_MODE_REPLIES = {
    "all": "📢 Режим сповіщень змінено: <b>ALL (Усі фрази)</b>",
    "watch": "👀 Режим сповіщень змінено: <b>WATCH (Лише список спостереження)</b>",
    "commands": "⚡️ Режим сповіщень змінено: <b>COMMANDS (Команди й позивні)</b>",
    "off": "🔕 Автоматичні сповіщення вимкнено.",
}
# Команди й текстові синоніми режимів (російські форми — вхідні дані користувачів).
MODE_COMMANDS = {
    alias: (mode, _MODE_REPLIES[mode])
    for mode, aliases in {
        "all": ("/all", "all", "все", "всі"),
        "watch": ("/watch", "watch", "вач", "спостереження"),
        "commands": ("/commands", "commands", "команды", "cmd", "команди"),
        "off": ("/off", "off", "/silent", "тихо", "тиша"),
    }.items()
    for alias in aliases
}
MODE_NAMES = {
    "all": "усі фрази", "watch": "список спостереження",
    "commands": "команди й позивні", "off": "вимкнено",
}
MODE_HELP = {
    "admin": "/all, /watch, /commands, /off",
    "editor": "/watch, /commands, /off",
    "viewer": "/watch, /commands, /off",
}
ROLE_LABELS = {"viewer": "спостерігач", "editor": "редактор", "admin": "адміністратор"}
# Без токена — кнопка старої версії: вважається застарілою.
ACCESS_CALLBACK_RE = re.compile(r"acc:(viewer|editor|reject):([1-9][0-9]{0,18})(?::([0-9a-f]{8}))?")


class TelegramBase:
    def __init__(self, bot_token, sample_rate, db_path, log):
        self.recent_phrases_max = 200
        self.bot_token = bot_token
        self.sample_rate = sample_rate
        self.db_path = db_path
        self.api = f"https://api.telegram.org/bot{bot_token}" if bot_token else ""
        self.log = log
        self._updates_offset = None

    # ---------- HTTP ----------

    def _request(self, method, operation, **kwargs):
        started = time.monotonic()
        runtime.count(f"telegram.{operation}.attempts")
        outcome = "interrupted"
        try:
            response = getattr(requests, method)(f"{self.api}/{operation}", **kwargs)
            status = response.status_code
            outcome = f"http_{status}" if type(status) is int else "unclassified"
            runtime.count(f"telegram.{operation}.{outcome}")
            if operation in ("sendVoice", "sendMessage") and status == 200:
                try:
                    payload = response.json()
                except ValueError:
                    runtime.count(f"telegram.{operation}.invalid_json")
                else:
                    if isinstance(payload, dict) and type(payload.get("ok")) is bool:
                        result = "api_confirmed" if payload["ok"] else "api_rejected"
                        runtime.count(f"telegram.{operation}.{result}")
                        runtime.count(f"telegram.{result}")
            return response
        except requests.RequestException as exc:
            outcome = error_kind(exc)
            runtime.count(f"telegram.{operation}.{outcome}")
            if operation in ("sendVoice", "sendMessage"):
                runtime.count("telegram.delivery_uncertain")
            raise
        finally:
            elapsed = time.monotonic() - started
            runtime.observe(f"telegram.{operation}", elapsed)
            # No caption, user IDs, token, file IDs, URL or exception message.
            logging.getLogger("radio.diagnostics").info(
                "TG_DIAG operation=%s outcome=%s elapsed_s=%.3f", operation, outcome, elapsed,
            )

    def send_text(self, text: str, reply_markup: str | None = None, parse_mode: str | None = "HTML", *, chat_id):
        """Надсилає текст в особистий чат. True — Telegram прийняв повідомлення."""
        if not (self.bot_token and chat_id):
            return False
        data = {"chat_id": chat_id, "text": text[:4000]}
        if parse_mode is not None:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup
        try:
            r = self._request("post", "sendMessage", data=data, timeout=15)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            self.log.error("Telegram (текст) не надіслано: %s", e)
            return False

    def _answer_callback(self, cq, text):
        try:
            self._request("post", "answerCallbackQuery",
                          data={"callback_query_id": cq.get("id"), "text": text}, timeout=15)
        except requests.RequestException:
            pass

    # ---------- Ролі з бази ----------

    @staticmethod
    def _private_sender(msg_or_cq, chat):
        """ID користувача, якщо це його особистий чат із ботом."""
        sender = msg_or_cq.get("from") or {}
        if chat.get("type") != "private":
            return None
        user_id = sender.get("id")
        if type(user_id) is not int or str(user_id) != str(chat.get("id")):
            return None
        return user_id

    def _user_state(self, user_id):
        """(роль, режим) активного користувача або None (також при помилці БД)."""
        try:
            return load_user_mode(self.db_path, int(user_id))
        except (sqlite3.Error, ValueError, TypeError):
            return None

    def _admin_chats(self):
        """Особисті чати всіх активних адміністраторів."""
        try:
            return [u["chat_id"] for u in list_recipients(self.db_path) if u["role"] == "admin"]
        except sqlite3.Error:
            return []

    # ---------- Клавіатури ----------

    def get_keyboard(self, fix_id=None):
        """Видалення та редактор Mini App (для фраз, які можна виправити)."""
        delete_data = f"del:{fix_id}" if fix_id and FIX_RE.fullmatch(fix_id) else "del"
        rows = [[{"text": "🗑 Видалити", "callback_data": delete_data}]]
        if fix_id is not None and (fix_id.startswith("db:") or re.fullmatch(r"mem:[0-9a-f]{32}", fix_id)):
            rows.append([{
                "text": "✏️ Виправити в редакторі",
                "web_app": {"url": MINIAPP_URL + "?fix_id=" + quote(fix_id, safe="")},
            }])
        return json.dumps({"inline_keyboard": rows})

    def keyboard_for(self, role, fix_id):
        """Кнопки за роллю: редактор Mini App — для admin/editor, видалення — для всіх."""
        if role in ("admin", "editor"):
            return self.get_keyboard(fix_id)
        delete_data = f"del:{fix_id}" if fix_id and FIX_RE.fullmatch(fix_id) else "del"
        return json.dumps({"inline_keyboard": [[{"text": "🗑 Видалити", "callback_data": delete_data}]]})

    def _miniapp_markup(self):
        return json.dumps({"inline_keyboard": [[
            {"text": "📻 Відкрити Radio 345", "web_app": {"url": MINIAPP_URL}},
        ]]})

    # ---------- Службові повідомлення ----------

    def notify_admin_async(self, text, delays=(0, 15, 60, 180)):
        """Службове повідомлення всім активним адміністраторам у фоні, з повторами.

        Після пробудження Mac мережа з’являється не одразу, тож надсилаємо
        кількома спробами й ніколи не блокуємо потік аудіо.
        """
        if not self.bot_token:
            return None

        def deliver():
            delivered = set()
            for delay in delays:
                if delay:
                    time.sleep(delay)
                # База може бути тимчасово недоступна; повторюємо перевірку,
                # але не надсилаємо за старими правами й не дублюємо успішні доставки.
                admins = self._admin_chats()
                if not admins:
                    continue
                for chat_id in admins:
                    if chat_id in delivered:
                        continue
                    state = self._user_state(chat_id)
                    if state is None or state[0] != "admin":
                        continue
                    try:
                        r = self._request("post", "sendMessage", data={
                            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                        }, timeout=15)
                        if r.status_code == 200:
                            delivered.add(chat_id)
                    except requests.RequestException:
                        pass
                if all(chat_id in delivered for chat_id in admins):
                    runtime.count("telegram.alert_sent")
                    return
            runtime.count("telegram.alert_failed")
            self.log.warning("Службове повідомлення адміністраторам не надіслано")

        thread = threading.Thread(target=deliver, name="telegram-alert", daemon=True)
        thread.start()
        return thread
