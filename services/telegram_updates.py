"""Приймання оновлень Telegram: розбір, кнопки повідомлень і long polling."""

import json
import os
from pathlib import Path
import sqlite3
import time

import requests

from core.diagnostics import error_kind, runtime
from services.telegram_sync import forget_delivery
from services.telegram_media_store import callback_fix_id, deletion_intent, deletion_result
from services.telegram_offset_store import load_telegram_offset, save_telegram_offset
from services.telegram_retry import listener_retry_delay


_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)


def _network_reason(error):
    """Стабільний короткий опис без URL, токена чи тексту винятку."""
    reasons = {
        "connection_reset": "обрив з’єднання",
        "ReadTimeout": "тайм-аут відповіді",
        "ConnectTimeout": "тайм-аут з’єднання",
        "Timeout": "тайм-аут",
        "ConnectionError": "помилка з’єднання",
    }
    return reasons.get(error_kind(error),
                       "помилка TLS" if isinstance(error, requests.exceptions.SSLError) else "помилка з’єднання")


class UpdatesMixin:
    def handle_update(self, upd):
        cq = upd.get("callback_query")
        if cq:
            self._handle_callback(cq)
            return
        msg = upd.get("message")
        if not msg:
            return
        # /start приймається від будь-кого: так нові користувачі просять доступ.
        if self._handle_start(msg):
            return
        if self._handle_wishlist_message(msg):
            return
        if self._handle_user_mode_command(msg, upd.get("update_id")):
            self.clean_command(msg)

    # ---------- Кнопки ----------

    def _handle_callback(self, cq):
        """Кнопки приймаються лише в особистому чаті того, хто їх натиснув."""
        message = cq.get("message") or {}
        actor = self._private_sender(cq, message.get("chat") or {})
        if actor is None:
            return
        chat_id = str(actor)
        data = cq.get("data", "")
        if data.startswith("acc:"):
            # Права адміністратора перевіряє resolve_request.
            self._handle_access_decision(cq, chat_id, message.get("message_id"), data)
            return
        if data == "del" or data.startswith("del:"):
            state = self._user_state(actor)
            from services.user_activity import record as record_activity
            if state is not None and state[0] == "admin":
                self._hide_phrase(cq, chat_id, message, data)
                record_activity(self.db_path, actor, "hide", data[4:])
            else:
                self._delete_own_copy(cq, chat_id, message)
                record_activity(self.db_path, actor, "delete", data[4:])

    def _delete_own_copy(self, cq, chat_id, message):
        """Видаляє лише повідомлення в чаті користувача; фраза лишається для інших."""
        success = False
        try:
            r = self._request("post", "deleteMessage",
                              data={"chat_id": chat_id, "message_id": message.get("message_id")}, timeout=15)
            result = r.json()
            success = isinstance(result, dict) and (
                (r.status_code == 200 and result.get("ok") is True)
                or result.get("description") == "Bad Request: message to delete not found")
        except (requests.RequestException, ValueError, AttributeError):
            pass
        if success:
            forget_delivery(self.db_path, chat_id, message.get("message_id"))
        self._answer_callback(cq, "Видалено" if success else "Помилка")

    def _hide_phrase(self, cq, chat_id, message, data):
        """Адміністратор: прибрати повідомлення й приховати фразу в журналі для всіх."""
        failure = None
        try:
            fix_id = callback_fix_id(self.db_path, data, message)
            if fix_id:
                # Намір переживає втрату відповіді після фактичного видалення.
                deletion_intent(self.db_path, fix_id)
            response = self._request(
                "post", "deleteMessage",
                data={"chat_id": chat_id, "message_id": message.get("message_id")},
                timeout=15,
            )
            result = response.json()
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("Некоректна відповідь Telegram на видалення")
            success = (result["ok"] is True
                       or result.get("description") == "Bad Request: message to delete not found")
            if fix_id:
                deletion_result(self.db_path, fix_id, success)
            runtime.count("telegram.admin_delete.confirmed" if success else "telegram.admin_delete.rejected")
        except Exception as exc:
            runtime.count("telegram.admin_delete.unconfirmed")
            failure = exc
        if failure is not None:
            # Відповідаємо поза except, щоб окремий збій answerCallbackQuery не
            # успадкував причину deleteMessage у мережевій діагностиці.
            # Вихідну помилку передаємо наявним повторам; стан deleting збережено.
            # Збій мережі зазвичай минає: повтор через 5 с, тож чесно кажемо про це.
            network = isinstance(failure, _NETWORK_ERRORS)
            self._answer_callback(cq, "Немає зв’язку — повторюю" if network else "Помилка")
            raise failure
        self._answer_callback(cq, "Видалено" if success else "Помилка")

    # ---------- Long polling ----------

    def _quarantine_update(self, update):
        """Зберігає необроблене оновлення для ручного відновлення."""
        path = Path(self.db_path).expanduser().resolve().parent / "telegram_failed_updates.jsonl"
        line = json.dumps(update, ensure_ascii=False) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())

    def _advance_offset(self, update_id):
        next_offset = update_id + 1
        # Спершу SQLite, лише потім оновлення вважається підтвердженим у пам’яті.
        save_telegram_offset(self.db_path, next_offset)
        self._updates_offset = next_offset

    def callback_listener(self):
        """Слухає кнопки, /start і команди режимів."""
        if self._updates_offset is None:
            self._updates_offset = load_telegram_offset(self.db_path)
        consecutive_errors = 0
        while True:
            try:
                r = self._request(
                    "get", "getUpdates",
                    params={
                        "timeout": 30,
                        "offset": self._updates_offset,
                        "allowed_updates": json.dumps(["callback_query", "message"]),
                    },
                    timeout=40,
                )
                if r.status_code != 200:
                    consecutive_errors += 1
                    delay = listener_retry_delay(consecutive_errors)
                    self.log.warning(
                        "Telegram getUpdates: HTTP %s; повтор через %s с (помилок поспіль: %s)",
                        r.status_code, delay, consecutive_errors,
                    )
                    time.sleep(delay)
                    continue
                if consecutive_errors:
                    self.log.info(
                        "Зв’язок із Telegram відновлено "
                        "після %s помилок опитування",
                        consecutive_errors,
                    )
                    consecutive_errors = 0

                for upd in r.json().get("result", []):
                    try:
                        update_id = upd["update_id"]
                        if type(update_id) is not int:
                            raise TypeError("update_id має бути цілим числом")
                    except (KeyError, TypeError):
                        self.log.exception("Telegram надіслав update без коректного update_id")
                        continue

                    # Захист від повторного отримання вже підтвердженого оновлення.
                    if self._updates_offset is not None and update_id < self._updates_offset:
                        runtime.count("telegram.duplicate_update_skipped")
                        continue

                    try:
                        self.handle_update(upd)
                    except Exception as exc:
                        runtime.count("telegram.update_processing_error")
                        retries = getattr(self, "_update_retries", None)
                        if retries is None:
                            retries = {}
                            self._update_retries = retries
                        attempts = retries.get(update_id, 0) + 1
                        retries[update_id] = attempts
                        if isinstance(exc, _NETWORK_ERRORS):
                            next_step = "повтор через 5 с" if attempts < 3 else "спроби вичерпано"
                            self.log.warning("Telegram недоступний (%s); %s, спроба %s/3",
                                             _network_reason(exc), next_step, attempts)
                        else:
                            self.log.exception("Помилка Telegram update_id=%s, спроба %s/3", update_id, attempts)
                        if attempts < 3:
                            time.sleep(5)
                            break
                        try:
                            self._quarantine_update(upd)
                            runtime.count("telegram.update_quarantined")
                        except Exception:
                            runtime.count("telegram.quarantine_write_failed")
                            self.log.exception(
                                "Не вдалося зберегти проблемний Telegram update. Offset не змінено."
                            )
                            time.sleep(5)
                            break
                        self._advance_offset(update_id)
                        retries.pop(update_id, None)
                        self.log.critical(
                            "Telegram update_id=%s збережено в telegram_failed_updates.jsonl. "
                            "Потрібна ручна перевірка.",
                            update_id,
                        )
                    else:
                        self._advance_offset(update_id)
                        retries = getattr(self, "_update_retries", None)
                        if retries is not None:
                            retries.pop(update_id, None)

            except (requests.RequestException, ValueError, KeyError, sqlite3.Error) as e:
                # sqlite3.Error: offset не збережено. Повторна обробка того самого
                # update безпечна — команди й рішення ідемпотентні.
                consecutive_errors += 1
                delay = listener_retry_delay(consecutive_errors)
                log_failure = self.log.warning if isinstance(e, _NETWORK_ERRORS) else self.log.exception
                log_failure(
                    "Збій зв’язку в callback_listener: %s; "
                    "повтор через %s с (помилок поспіль: %s)",
                    _network_reason(e) if isinstance(e, _NETWORK_ERRORS) else type(e).__name__,
                    delay, consecutive_errors,
                )
                time.sleep(delay)
