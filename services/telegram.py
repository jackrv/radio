import json
import queue
import html
import logging
import sqlite3
import re
import subprocess
import threading
import time
import secrets
from urllib.parse import quote

import requests
from core.diagnostics import runtime, error_kind

from services.access_requests import mark_undelivered, profile_of, register_request, resolve_request
from services.telegram_phrase_store import register_phrase
from services.user_notify_store import (
    apply_telegram_user_mode, has_any_user, list_recipients,
    load_user_mode, save_user_mode,
)
from services.telegram_media_store import (
    remember_message, remember_voice, callback_fix_id, deletion_intent, deletion_result, FIX_RE,
)
from services.telegram_retry import retry_delay, listener_retry_delay
from services.telegram_offset_store import (
    load_telegram_offset,
    save_telegram_offset,
)

MINIAPP_URL = "https://jackrv.github.io/radio/"
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
ACCESS_CALLBACK_RE = re.compile(r"acc:(viewer|editor|reject):([1-9][0-9]{0,18})")


class TelegramService:
    def __init__(
    self,
    bot_token,
    chat_id,
    sample_rate,
    db_path,
    log,
    state_lock,
    get_notify_mode,
    set_notify_mode,
    apply_telegram_mode=None,
):
        self.recent_phrases_max = 200
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.sample_rate = sample_rate
        self.db_path = db_path
        self.api = (
            f"https://api.telegram.org/bot{bot_token}"
            if bot_token else ""
        )
        self.log = log
        self.state_lock = state_lock
        self.get_notify_mode = get_notify_mode
        self.set_notify_mode = set_notify_mode
        self.apply_telegram_mode = apply_telegram_mode
        self._updates_offset = None

        # Ограниченная очередь исходящих голосовых уведомлений.
        self._outgoing_queue = queue.Queue(maxsize=32)
        self._outgoing_lock = threading.Lock()
        self._outgoing_thread = None
        self._outgoing_stop = threading.Event()

    def get_keyboard(self, fix_id=None):
        """Кнопки управления сообщением."""
        delete_data = f"del:{fix_id}" if fix_id and FIX_RE.fullmatch(fix_id) else "del"
        row = [{"text": "🗑 Видалити", "callback_data": delete_data}]
        rows = [row]
        if fix_id is not None:
            if (
                fix_id.startswith("db:")
                or re.fullmatch(r"mem:[0-9a-f]{32}", fix_id)
            ):
                # Web App buttons are supported in private Telegram chats.
                rows.append([{
                    "text": "✏️ Виправити в редакторі",
                    "web_app": {
                        "url": MINIAPP_URL + "?fix_id=" + quote(fix_id, safe=""),
                    },
                }])
        return json.dumps({"inline_keyboard": rows})

    def send_text(self, text: str, reply_markup: str | None = None, parse_mode: str | None = "HTML", *, chat_id=None):
        """Надсилає текст у робочий чат або, якщо задано ``chat_id``, в інший особистий чат.

        Повертає True, якщо Telegram прийняв повідомлення.
        """
        target = chat_id or self.chat_id
        if not (self.bot_token and target):
            return False

        data = {
            "chat_id": target,
            "text": text[:4000],
        }
        if parse_mode is not None:
            data["parse_mode"] = parse_mode

        if reply_markup:
            data["reply_markup"] = reply_markup

        try:
            r = self._request("post", "sendMessage",
                data=data,
                timeout=15,
            )
            r.raise_for_status()
            # Прив’язка фрази — лише до повідомлення робочого чату.
            if reply_markup and str(target) == str(self.chat_id):
                try:
                    markup = json.loads(reply_markup)
                    for row in markup.get("inline_keyboard", []):
                        for button in row:
                            data = button.get("callback_data", "")
                            if data.startswith("del:"):
                                self._remember_sent(r, data[4:])
                except (TypeError, ValueError):
                    pass
            return True
        except requests.RequestException as e:
            self.log.error("Telegram (текст) не надіслано: %s", e)
            return False

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

    def _remember_sent(self, response, fix_id):
        if not fix_id:
            return
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("ok") is True:
                remember_message(self.db_path, fix_id, payload.get("result"))
        except (sqlite3.Error, ValueError):
            # Do not retry an already sent message when local metadata fails.
            self.log.warning("Не вдалося зберегти зв’язок повідомлення Telegram із фразою")

    def pcm_to_ogg(self, pcm: bytes) -> bytes:
        proc = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error",
                "-f", "s16le",
                "-ar", str(self.sample_rate),
                "-ac", "1",
                "-i", "pipe:0",
                "-c:a", "libopus",
                "-b:a", "32k",
                "-f", "ogg",
                "pipe:1",
            ],
            input=pcm,
            capture_output=True,
            check=True,
        )
        return proc.stdout

    def _notifications_enabled(self):
        with self.state_lock:
            return self.get_notify_mode() != "off"

    def send_voice(self, pcm: bytes, caption: str, fix_id=None, recipients=None):
        if recipients is not None:
            self._send_voice_to_recipients(pcm, caption, fix_id, recipients)
            return
        if not self._notifications_enabled():
            return

        reply_markup = self.get_keyboard(fix_id)

        try:
            with runtime.timed("telegram.ogg_conversion"):
                ogg = self.pcm_to_ogg(pcm)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            runtime.count("telegram.conversion_failed")
            runtime.count("telegram.text_fallback")
            self.log.warning(
                "Конвертація в OGG не вдалася (%s), надсилаю текстом", e
            )
            if self._notifications_enabled():
                self.send_text(caption, reply_markup, parse_mode=None)
            return

        duration = max(1, round(len(pcm) / (2 * self.sample_rate)))

        for attempt in range(1, 4):
            if not self._notifications_enabled():
                return

            try:
                r = self._request("post", "sendVoice",
                    data={
                        "chat_id": self.chat_id,
                        "caption": caption[:1000],
                        "duration": duration,
                        "reply_markup": reply_markup,
                    },
                    files={"voice": ("phrase.ogg", ogg, "audio/ogg")},
                    timeout=(10, 60),
                )
                r.raise_for_status()
                self._remember_sent(r, fix_id)
                return
            except requests.RequestException as e:
                self.log.warning(
                    "Спроба %d/3 надіслати голосове не вдалася: %s",
                    attempt, e,
                )
                if attempt == 3:
                    runtime.count("telegram.voice_exhausted")
                    runtime.count("telegram.text_fallback")
                    self.log.error(
                        "Усі спроби вичерпано, надсилаю резервним текстом"
                    )
                    if self._notifications_enabled():
                        self.send_text(
                            caption, reply_markup, parse_mode=None
                        )
                else:
                    delay = retry_delay(attempt, e)
                    self.log.warning(
                        "Повтор Telegram через %s сек.", delay
                    )
                    time.sleep(delay)

    def recipients_for(self, wants):
        """Отримувачі фрази за особистими режимами.

        ``wants(mode)`` — чи підходить фраза під режим. Поки в базі немає
        жодного користувача, робочий чат із .env отримує повідомлення за
        загальним режимом. Після реєстрації отримувачів визначає лише база:
        відключений власник .env більше нічого не отримує.
        """
        try:
            users = list_recipients(self.db_path)
            registered = has_any_user(self.db_path)
        except sqlite3.Error:
            self.log.exception("Не вдалося прочитати отримувачів сповіщень")
            users, registered = [], False
        if self.chat_id and not registered:
            with self.state_lock:
                mode = self.get_notify_mode()
            users.append({"chat_id": self.chat_id, "role": "admin", "mode": mode, "legacy": True})
        selected = [u for u in users if u["mode"] != "off" and wants(u["mode"])]
        # Робочий чат першим: його повідомлення прив’язується до фрази.
        selected.sort(key=lambda u: str(u["chat_id"]) != str(self.chat_id))
        return selected

    def keyboard_for(self, role, fix_id):
        """Кнопки за роллю: редактор Mini App — для admin/editor, видалення — для всіх."""
        if role in ("admin", "editor"):
            return self.get_keyboard(fix_id)
        delete_data = f"del:{fix_id}" if fix_id and FIX_RE.fullmatch(fix_id) else "del"
        return json.dumps({"inline_keyboard": [[{"text": "🗑 Видалити", "callback_data": delete_data}]]})

    def _recipient_active(self, recipient):
        if recipient.get("legacy"):
            return self._notifications_enabled()
        try:
            state = load_user_mode(self.db_path, int(recipient["chat_id"]))
        except (sqlite3.Error, ValueError):
            return False
        return state is not None and state[1] != "off"

    def _send_voice_to_recipients(self, pcm, caption, fix_id, recipients):
        active = [r for r in recipients if self._recipient_active(r)]
        if not active:
            runtime.count("telegram.queue_discarded_off")
            return
        try:
            with runtime.timed("telegram.ogg_conversion"):
                ogg = self.pcm_to_ogg(pcm)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            runtime.count("telegram.conversion_failed")
            self.log.warning("Конвертація в OGG не вдалася (%s), надсилаю текстом", e)
            for recipient in active:
                if self._recipient_active(recipient):
                    runtime.count("telegram.text_fallback")
                    self.send_text(caption, self.keyboard_for(recipient["role"], fix_id),
                                   parse_mode=None, chat_id=recipient["chat_id"])
            return

        duration = max(1, round(len(pcm) / (2 * self.sample_rate)))
        voice_ref = None  # file_id першого надісланого: далі без повторного завантаження
        for recipient in active:
            sent = self._deliver_voice(recipient, ogg, voice_ref, caption, duration, fix_id)
            if voice_ref is None and sent:
                voice_ref = sent

    def _deliver_voice(self, recipient, ogg, voice_ref, caption, duration, fix_id):
        """Надсилає голосове одному отримувачу. Повертає file_id або None."""
        chat_id = recipient["chat_id"]
        work_chat = str(chat_id) == str(self.chat_id)
        reply_markup = self.keyboard_for(recipient["role"], fix_id)
        for attempt in range(1, 4):
            if not self._recipient_active(recipient):
                return None
            data = {"chat_id": chat_id, "caption": caption[:1000],
                    "duration": duration, "reply_markup": reply_markup}
            kwargs = {"data": data, "timeout": (10, 60)}
            if voice_ref:
                data["voice"] = voice_ref
            else:
                kwargs["files"] = {"voice": ("phrase.ogg", ogg, "audio/ogg")}
            try:
                r = self._request("post", "sendVoice", **kwargs)
                r.raise_for_status()
            except requests.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 403:
                    # Користувач не запускав бота або заблокував його: повтори марні.
                    runtime.count("telegram.recipient_blocked")
                    self.log.warning("Telegram відхилив надсилання користувачу (403); пропущено")
                    return None
                self.log.warning("Спроба %d/3 надіслати голосове не вдалася: %s", attempt, e)
                if attempt == 3:
                    runtime.count("telegram.voice_exhausted")
                    runtime.count("telegram.text_fallback")
                    self.log.error("Усі спроби вичерпано, надсилаю резервним текстом")
                    if self._recipient_active(recipient):
                        self.send_text(caption, reply_markup, parse_mode=None,
                                       chat_id=None if work_chat else chat_id)
                    return None
                delay = retry_delay(attempt, e)
                self.log.warning("Повтор Telegram через %s сек.", delay)
                time.sleep(delay)
                continue

            file_id = None
            try:
                message = (r.json() or {}).get("result") or {}
                file_id = (message.get("voice") or {}).get("file_id")
            except (ValueError, AttributeError):
                pass
            try:
                if work_chat:
                    self._remember_sent(r, fix_id)
                else:
                    remember_voice(self.db_path, fix_id, file_id)
            except sqlite3.Error:
                self.log.warning("Не вдалося зберегти зв’язок голосового з фразою")
            return file_id if isinstance(file_id, str) else None
        return None

    def notify_admin_async(self, text, delays=(0, 15, 60, 180)):
        """Службове повідомлення в робочий чат у фоні, з повторами.

        Після пробудження Mac мережа з’являється не одразу, тож надсилаємо
        кількома спробами й ніколи не блокуємо потік аудіо.
        """
        if not (self.bot_token and self.chat_id):
            return None

        def deliver():
            for delay in delays:
                if delay:
                    time.sleep(delay)
                try:
                    r = self._request("post", "sendMessage", data={
                        "chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML",
                    }, timeout=15)
                    if r.status_code == 200:
                        runtime.count("telegram.alert_sent")
                        return
                except requests.RequestException:
                    pass
            runtime.count("telegram.alert_failed")
            self.log.warning("Службове повідомлення адміністратору не надіслано")

        thread = threading.Thread(target=deliver, name="telegram-alert", daemon=True)
        thread.start()
        return thread

    def _outgoing_loop(self):
        """Отправляет сообщения и завершается после опустошения очереди."""
        while True:
            if (
                self._outgoing_stop.is_set()
                and self._outgoing_queue.empty()
            ):
                return

            try:
                queued_at, item = self._outgoing_queue.get(timeout=0.2)
                runtime.observe("telegram.queue_wait", time.monotonic() - queued_at)
                runtime.gauge("telegram.queue_depth", self._outgoing_queue.qsize())
            except queue.Empty:
                continue

            try:
                if len(item) == 4:
                    # Особисті режими перевіряються для кожного отримувача.
                    self.send_voice(*item)
                elif self._notifications_enabled():
                    self.send_voice(*item)
                else:
                    runtime.count("telegram.queue_discarded_off")
                    self.log.info(
                        "Сповіщення з черги відкинуто: режим OFF"
                    )
            except Exception:
                runtime.count("telegram.worker_error")
                self.log.exception(
                    "Помилка фонового надсилання Telegram"
                )
            finally:
                self._outgoing_queue.task_done()

    def shutdown(self, timeout=10.0):
        """Прекращает приём и ожидает завершения отправки."""
        with self._outgoing_lock:
            self._outgoing_stop.set()
            thread = self._outgoing_thread

        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

        pending = self._outgoing_queue.unfinished_tasks
        runtime.gauge("telegram.shutdown_pending", pending)

        if pending:
            self.log.warning(
                "Telegram: завершення з %d ненадісланими "
                "або незавершеними сповіщеннями",
                pending,
            )
            return False

        self.log.info("Чергу Telegram оброблено")
        return True

    def notify(self, watch_str, to_str, from_str, text, pcm, fix_id=None, recipients=None):
        runtime.count("telegram.notify_requested")
        if not (self.bot_token and self.chat_id):
            runtime.count("telegram.notify_unconfigured")
            return

        if recipients is not None:
            if not recipients:
                runtime.count("telegram.notify_skipped_off")
                return
        elif not self._notifications_enabled():
            runtime.count("telegram.notify_skipped_off")
            return

        lines = [f"Подія: {watch_str}"]
        if from_str or to_str:
            lines.append(f"Кому: {to_str or '-'} | Від: {from_str or '-'}")

        caption = "\n".join(lines) + f"\n\n{text}"

        with self._outgoing_lock:
            if self._outgoing_stop.is_set():
                runtime.count("telegram.notify_after_shutdown")
                self.log.warning(
                    "Telegram завершує роботу: сповіщення пропущено"
                )
                return

            try:
                item = (pcm, caption, fix_id) if recipients is None else (pcm, caption, fix_id, list(recipients))
                self._outgoing_queue.put_nowait((time.monotonic(), item))
                runtime.count("telegram.enqueued")
                runtime.gauge("telegram.queue_depth", self._outgoing_queue.qsize())
            except queue.Full:
                runtime.count("telegram.queue_overflow")
                self.log.error(
                    "Очередь Telegram переполнена (32): "
                    "новое сповіщення пропущено"
                )
                return

            if (
                self._outgoing_thread is None
                or not self._outgoing_thread.is_alive()
            ):
                self._outgoing_thread = threading.Thread(
                    target=self._outgoing_loop,
                    name="telegram-outbox",
                    daemon=True,
                )
                self._outgoing_thread.start()

    def register_phrase_for_fix(self, text: str, event_id: int | None) -> str | None:
        """Ідентифікатор фрази для редактора Mini App.

        ``db:<id>`` — подія SQLite; ``mem:<token>`` — тимчасова фраза на 24 години.
        None — фразу не вдалося зберегти: повідомлення матиме лише «Видалити».
        """
        if event_id is not None:
            return f"db:{event_id}"
        if not (self.bot_token and self.chat_id):
            return None

        token = secrets.token_hex(16)
        try:
            register_phrase(
                self.db_path, token, text,
                max_phrases=min(self.recent_phrases_max, 200),
            )
        except (sqlite3.Error, ValueError) as exc:
            self.log.warning(
                "Не вдалося зберегти тимчасову фразу в SQLite: %s; "
                "виправлення в редакторі недоступне", exc,
            )
            return None
        return f"mem:{token}"

    def handle_update(self, upd):
        cq = upd.get("callback_query")

        if cq:
            chat_id = str(
                ((cq.get("message") or {}).get("chat") or {}).get("id", "")
            )
            if chat_id != str(self.chat_id):
                self._handle_member_callback(cq, chat_id)
                return

            data = cq.get("data", "")
            msg_id = (cq.get("message") or {}).get("message_id")

            if data.startswith("acc:"):
                self._handle_access_decision(cq, chat_id, msg_id, data)
                return

            if (data == "del" or data.startswith("del:")) and not self._may_hide_phrases(cq):
                # Не адміністратор: прибирається лише власна копія повідомлення.
                self._handle_member_callback(cq, chat_id)
                return

            if data == "del" or data.startswith("del:"):
                fix_id = callback_fix_id(self.db_path, data, cq.get("message") or {})
                if fix_id:
                    # Durable intent survives a crash after Telegram deletes the message.
                    deletion_intent(self.db_path, fix_id)
                d = requests.post(
                    f"{self.api}/deleteMessage",
                    data={"chat_id": chat_id, "message_id": msg_id},
                    timeout=15,
                ).json()

                success = d.get("ok") is True or d.get("description") == "Bad Request: message to delete not found"
                if fix_id:
                    deletion_result(self.db_path, fix_id, success)

                requests.post(
                    f"{self.api}/answerCallbackQuery",
                    data={
                        "callback_query_id": cq["id"],
                        "text": "Видалено" if success else "Помилка",
                    },
                    timeout=15,
                )
            return

        msg = upd.get("message")
        if not msg:
            return

        # /start приймається з будь-якого особистого чату: так нові
        # користувачі просять доступ, не знаючи свого Telegram ID.
        if self._handle_start(msg):
            return
        if self._handle_user_mode_command(msg, upd.get("update_id")):
            return

        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(self.chat_id):
            return

        text_msg = (msg.get("text") or "").strip().lower()

        modes = MODE_COMMANDS

        if text_msg in modes and self._registered_users():
            # Особисті режими обробляються вище; відключеним — нічого.
            return

        if text_msg in modes:
            mode, response = modes[text_msg]
            update_id = upd.get("update_id")

            with self.state_lock:
                if (
                    self.apply_telegram_mode is not None
                    and type(update_id) is int
                ):
                    applied = self.apply_telegram_mode(
                        mode, update_id
                    )
                else:
                    # Совместимость с прямыми вызовами handle_update
                    # без update_id, в том числе в старых тестах.
                    self.set_notify_mode(mode)
                    applied = True

            if not applied:
                self.log.info(
                    "Повторну Telegram-команду %s пропущено",
                    update_id,
                )
                return

            self.log.info("Режим змінено на: %s", mode)
            self.send_text(response)
            return

    @staticmethod
    def _private_sender(msg_or_cq, chat):
        """ID користувача, якщо це його особистий чат із ботом."""
        sender = msg_or_cq.get("from") or {}
        if chat.get("type") not in (None, "private"):
            return None
        user_id = sender.get("id")
        if type(user_id) is not int or str(user_id) != str(chat.get("id")):
            return None
        return user_id

    def _handle_member_callback(self, cq, chat_id):
        """Кнопки в особистих чатах користувачів: лише видалення власної копії."""
        message = cq.get("message") or {}
        data = cq.get("data", "")
        if self._private_sender(cq, message.get("chat") or {}) is None:
            return
        if data.startswith("acc:"):
            # Картки заявок отримують усі адміністратори; права перевіряє resolve_request.
            self._handle_access_decision(cq, chat_id, message.get("message_id"), data)
            return
        if not (data == "del" or data.startswith("del:")):
            return
        success = False
        try:
            r = self._request("post", "deleteMessage",
                data={"chat_id": chat_id, "message_id": message.get("message_id")}, timeout=15)
            success = r.status_code == 200
        except requests.RequestException:
            pass
        self._answer_callback(cq, "Видалено" if success else "Помилка")

    def _handle_user_mode_command(self, msg, update_id):
        """Особистий режим: /all, /watch, /commands, /off від користувача.

        У робочому чаті без зареєстрованого користувача діє загальний режим
        (повертаємо False, щоб спрацювала стара обробка).
        """
        text = (msg.get("text") or "").strip().lower()
        if text not in MODE_COMMANDS:
            return False
        chat = msg.get("chat") or {}
        user_id = self._private_sender(msg, chat)
        work_chat = str(chat.get("id", "")) == str(self.chat_id)
        if user_id is None:
            return False
        try:
            state = load_user_mode(self.db_path, user_id)
        except sqlite3.Error:
            self.log.exception("Не вдалося прочитати режим користувача")
            return not work_chat
        if state is None:
            # Сторонніх і відключених ігноруємо мовчки; доступ просять через /start.
            # Загальний режим робочого чату діє, лише поки користувачів немає.
            return not (work_chat and not self._registered_users())

        mode, response = MODE_COMMANDS[text]
        target = None if work_chat else user_id
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
                chat_id=target,
            )
            return True
        except LookupError:
            return True
        if not applied:
            self.log.info("Повторну Telegram-команду %s пропущено", update_id)
            return True
        self.log.info("Особистий режим користувача змінено на: %s", mode)
        self.send_text(response, chat_id=target)
        return True

    def _registered_users(self):
        try:
            return has_any_user(self.db_path)
        except sqlite3.Error:
            return True  # обережно: без бази привілеїв .env не надаємо

    def _may_hide_phrases(self, cq):
        """Приховати фразу для всіх може лише активний адміністратор."""
        if not self._registered_users():
            return True
        actor = (cq.get("from") or {}).get("id")
        if type(actor) is not int:
            return False
        try:
            state = load_user_mode(self.db_path, actor)
        except (sqlite3.Error, ValueError):
            return False
        return state is not None and state[0] == "admin"

    def _admin_chats(self):
        """Чати для карток заявок: усі активні адміністратори або робочий чат."""
        try:
            admins = [u["chat_id"] for u in list_recipients(self.db_path) if u["role"] == "admin"]
        except sqlite3.Error:
            admins = []
        return admins or ([self.chat_id] if self.chat_id else [])

    def _mode_hint(self, user_id, role):
        try:
            state = load_user_mode(self.db_path, user_id)
        except sqlite3.Error:
            state = None
        current = f"Ваш режим сповіщень: {MODE_NAMES[state[1]]}. " if state else ""
        return f"{current}Змінити: {MODE_HELP.get(role, MODE_HELP['viewer'])}."

    def _miniapp_markup(self):
        return json.dumps({"inline_keyboard": [[
            {"text": "📻 Відкрити Radio 345", "web_app": {"url": MINIAPP_URL}},
        ]]})

    def _handle_start(self, msg):
        """Обробляє /start в особистому чаті. Повертає True, якщо це був /start."""
        words = (msg.get("text") or "").split()
        if not words or words[0].split("@", 1)[0].lower() != "/start":
            return False
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or type(sender.get("id")) is not int or sender["id"] != chat.get("id"):
            return False

        try:
            owner_state = load_user_mode(self.db_path, chat["id"])
        except sqlite3.Error:
            owner_state = None
        if str(chat["id"]) == str(self.chat_id) and (owner_state or not self._registered_users()):
            state = owner_state
            mode_line = f"Ваш режим сповіщень: {MODE_NAMES[state[1]]}.\n" if state else ""
            self.send_text(
                "📻 <b>Radio 345</b> на зв’язку.\n"
                f"{mode_line}Режими сповіщень: {MODE_HELP['admin']}.\n"
                "Запити на доступ нових користувачів надходитимуть сюди.",
                reply_markup=self._miniapp_markup(),
            )
            return True

        state, role = register_request(self.db_path, sender)
        user_chat = chat["id"]
        if state == "member":
            self.send_text(
                f"✅ У вас є доступ до Radio 345 (роль: {ROLE_LABELS.get(role, role)}).\n"
                + self._mode_hint(user_chat, role),
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

    @staticmethod
    def _access_card(user_id, name, username):
        lines = ["👤 <b>Запит на доступ до Radio 345</b>"]
        lines.append(f"Ім’я: {html.escape(name) if name else '—'}")
        if username:
            lines.append(f"Username: @{html.escape(username)}")
        lines.append(f"Telegram ID: <code>{user_id}</code>")
        return "\n".join(lines)

    def _notify_access_request(self, sender):
        name, username = profile_of(sender)
        user_id = sender["id"]
        markup = json.dumps({"inline_keyboard": [
            [
                {"text": "👁 Спостерігач", "callback_data": f"acc:viewer:{user_id}"},
                {"text": "✏️ Редактор", "callback_data": f"acc:editor:{user_id}"},
            ],
            [{"text": "✖️ Відхилити", "callback_data": f"acc:reject:{user_id}"}],
        ]})
        card = self._access_card(user_id, name, username)
        delivered = False
        for admin_chat in self._admin_chats():
            target = None if str(admin_chat) == str(self.chat_id) else admin_chat
            delivered = self.send_text(card, reply_markup=markup, chat_id=target) or delivered
        return delivered

    def _answer_callback(self, cq, text):
        try:
            self._request("post", "answerCallbackQuery",
                data={"callback_query_id": cq.get("id"), "text": text}, timeout=15)
        except requests.RequestException:
            pass

    def _handle_access_decision(self, cq, chat_id, msg_id, data):
        match = ACCESS_CALLBACK_RE.fullmatch(data)
        actor = (cq.get("from") or {}).get("id")
        if not match or type(actor) is not int:
            self._answer_callback(cq, "Некоректний запит")
            return
        action, user_id = match.group(1), int(match.group(2))
        try:
            result = resolve_request(self.db_path, actor, user_id, None if action == "reject" else action)
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

    def _quarantine_update(self, update):
        """Сохраняет необработанное обновление для ручного восстановления."""
        import os
        from pathlib import Path

        path = (
            Path(self.db_path).expanduser().resolve().parent
            / "telegram_failed_updates.jsonl"
        )

        line = json.dumps(update, ensure_ascii=False) + "\n"

        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )

        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())

    def _advance_offset(self, update_id):
        next_offset = update_id + 1

        # Сначала сохраняем в SQLite, только потом
        # считаем обновление подтверждённым в памяти.
        save_telegram_offset(self.db_path, next_offset)
        self._updates_offset = next_offset

    def callback_listener(self):
        """Слушает нажатия кнопок и команды управления режимами/обучением."""

        if self._updates_offset is None:
            self._updates_offset = load_telegram_offset(
                self.db_path
            )

        consecutive_errors = 0

        while True:
            try:
                r = self._request("get", "getUpdates",
                    params={
                        "timeout": 30,
                        "offset": self._updates_offset,
                        "allowed_updates": json.dumps(
                            ["callback_query", "message"]
                        ),
                    },
                    timeout=40,
                )

                if r.status_code != 200:
                    consecutive_errors += 1
                    delay = listener_retry_delay(consecutive_errors)

                    self.log.warning(
                        "Telegram getUpdates: HTTP %s; "
                        "повтор через %s с (помилок поспіль: %s)",
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
                        self.log.exception(
                            "Telegram надіслав update без коректного update_id"
                        )
                        continue

                    # Защита от повторного получения уже
                    # подтверждённого обновления.
                    if (
                        self._updates_offset is not None
                        and update_id < self._updates_offset
                    ):
                        runtime.count("telegram.duplicate_update_skipped")
                        continue

                    try:
                        self.handle_update(upd)

                    except Exception:
                        runtime.count("telegram.update_processing_error")
                        retries = getattr(self, "_update_retries", None)

                        if retries is None:
                            retries = {}
                            self._update_retries = retries

                        attempts = retries.get(update_id, 0) + 1
                        retries[update_id] = attempts

                        self.log.exception(
                            "Помилка Telegram update_id=%s, спроба %s/3",
                            update_id,
                            attempts,
                        )

                        if attempts < 3:
                            time.sleep(5)
                            break

                        try:
                            self._quarantine_update(upd)
                            runtime.count("telegram.update_quarantined")
                        except Exception:
                            runtime.count("telegram.quarantine_write_failed")
                            self.log.exception(
                                "Не вдалося зберегти проблемний Telegram update. "
                                "Offset не змінено."
                            )
                            time.sleep(5)
                            break

                        self._advance_offset(update_id)
                        retries.pop(update_id, None)

                        self.log.critical(
                            "Telegram update_id=%s збережено в "
                            "telegram_failed_updates.jsonl. "
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

                self.log.warning(
                    "Збій зв’язку в callback_listener: %s; "
                    "повтор через %s с (помилок поспіль: %s)",
                    e, delay, consecutive_errors,
                )
                time.sleep(delay)
