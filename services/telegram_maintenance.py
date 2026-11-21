"""Легке фонове обслуговування Telegram, без потоків розпізнавання."""

from contextvars import ContextVar
import sqlite3
import threading
import time

import requests

from core.diagnostics import runtime
from services.sqlite_connection import get_db
from services.telegram_sync import sync_corrections
from services.telegram_recall import process_deletions

managed_key = ContextVar("telegram_managed_key", default=None)


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_service_messages (
        chat_id INTEGER NOT NULL, slot TEXT NOT NULL, message_id INTEGER NOT NULL,
        expires_at REAL NOT NULL, PRIMARY KEY(chat_id,slot)
    )""")


class MaintenanceMixin:
    def _init_maintenance(self):
        self._service_lock = threading.RLock()
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = None

    def send_status(self, text, reply_markup=None, parse_mode="HTML", *, chat_id, slot="status", ttl=86400, fresh=False):
        # Keep send_text's bool contract and test/mocking boundary.
        token = managed_key.set((slot, ttl, fresh))
        try:
            with self._service_lock:
                kwargs = {"chat_id": chat_id}
                if reply_markup is not None:
                    kwargs["reply_markup"] = reply_markup
                if parse_mode != "HTML":
                    kwargs["parse_mode"] = parse_mode
                return self.send_text(text, **kwargs)
        finally:
            managed_key.reset(token)

    def _managed_text(self, data, slot, ttl, fresh=False):
        """Повторне використання картки; явний /start завжди створює нову."""
        with get_db(self.db_path) as db:
            ensure_table(db)
            row = db.execute("SELECT message_id FROM telegram_service_messages WHERE chat_id=? AND slot=?",
                             (data["chat_id"],slot)).fetchone()
        previous_message_id = row[0] if row else None
        if fresh:
            # Очищення історії клієнтом може не зробити старе повідомлення
            # недоступним для editMessageText. Новий /start потребує sendMessage.
            row = None
        if row:
            edit = {**data, "message_id": row[0]}
            edit.setdefault("reply_markup", '{"inline_keyboard":[]}')
            response = self._request("post", "editMessageText", data=edit, timeout=15)
            result = response.json()
            if not isinstance(result, dict):
                return False
            if result.get("ok") is True or str(result.get("description", "")).startswith("Bad Request: message is not modified"):
                message_id = row[0]
            elif result.get("description") in ("Bad Request: message to edit not found", "Bad Request: message can't be edited"):
                row = None
            else:
                return False  # Uncertain delivery: don't create another status card.
        if not row:
            response = self._request("post", "sendMessage", data=data, timeout=15)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
                return False
            message_id = result["result"].get("message_id")
            if result.get("ok") is not True or type(message_id) is not int:
                return False
        with get_db(self.db_path) as db:
            db.execute("""INSERT INTO telegram_service_messages VALUES (?,?,?,?)
                ON CONFLICT(chat_id,slot) DO UPDATE SET message_id=excluded.message_id,expires_at=excluded.expires_at""",
                       (data["chat_id"], slot, message_id, time.time()+ttl))
        if previous_message_id is not None and previous_message_id != message_id:
            # Спочатку підтвердження нового повідомлення та запис адреси в SQLite.
            # Прибирання старої копії не повинно скасовувати успішну відповідь.
            try:
                self._request("post", "deleteMessage", data={
                    "chat_id": data["chat_id"], "message_id": previous_message_id,
                }, timeout=(5, 10))
            except requests.RequestException:
                self.log.warning("Не вдалося прибрати попередню службову картку Telegram")
        return True

    def clean_command(self, msg):
        chat = self._private_sender(msg, msg.get("chat") or {})
        message = msg.get("message_id")
        if chat is None or type(message) is not int:
            return
        try:
            self._request("post", "deleteMessage", data={"chat_id":chat, "message_id":message}, timeout=(5,10))
        except requests.RequestException:
            self.log.warning("Не вдалося прибрати службову команду Telegram")

    def finish_access_cards(self, user_id):
        """All admins' obsolete approval buttons disappear, not just the clicked copy."""
        with get_db(self.db_path) as db:
            ensure_table(db)
            db.execute("UPDATE telegram_service_messages SET expires_at=0 WHERE slot=?", (f"access:{user_id}",))

    def maintenance_once(self):
        from services.telegram_recall import expire_deliveries
        expire_deliveries(self.db_path)
        process_deletions(self)
        sync_corrections(self)
        with get_db(self.db_path) as db:
            ensure_table(db)
            rows = db.execute("SELECT chat_id,slot,message_id FROM telegram_service_messages WHERE expires_at<=? LIMIT 5", (time.time(),)).fetchall()
        for chat, slot, message in rows:
            if self._maintenance_stop.is_set():
                break
            # Recheck under the same lock as replacement. Release it after each
            # message so a batch of slow deletes cannot monopolize /start replies.
            with self._service_lock:
                with get_db(self.db_path) as db:
                    due = db.execute("""SELECT 1 FROM telegram_service_messages
                        WHERE chat_id=? AND slot=? AND message_id=? AND expires_at<=?""",
                        (chat,slot,message,time.time())).fetchone()
                if not due:
                    continue
                response = self._request("post", "deleteMessage", data={"chat_id":chat,"message_id":message}, timeout=(5,10))
                result = response.json()
                if result.get("ok") is True or result.get("description") == "Bad Request: message to delete not found" or response.status_code == 403:
                    with get_db(self.db_path) as db:
                        db.execute("DELETE FROM telegram_service_messages WHERE chat_id=? AND slot=? AND message_id=?", (chat,slot,message))
                else:
                    with get_db(self.db_path) as db:
                        db.execute("UPDATE telegram_service_messages SET expires_at=? WHERE chat_id=? AND slot=?", (time.time()+60,chat,slot))

    def start_maintenance(self):
        if not self.bot_token or self._maintenance_stop.is_set():
            return
        if self._maintenance_thread is not None and self._maintenance_thread.is_alive():
            return
        def run():
            while not self._maintenance_stop.is_set():
                try:
                    self.maintenance_once()
                except (requests.RequestException, sqlite3.Error, ValueError, AttributeError):
                    self.log.warning("Фонове оновлення Telegram відкладено; повтор через 5 с")
                except Exception:
                    runtime.count("telegram.maintenance_error")
                    self.log.warning("Неочікувана помилка обслуговування Telegram; повтор через 5 с")
                self._maintenance_stop.wait(5)
        self._maintenance_thread = threading.Thread(target=run, name="telegram-maintenance", daemon=True)
        self._maintenance_thread.start()
