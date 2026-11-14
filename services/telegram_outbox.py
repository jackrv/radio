"""Розсилка голосових повідомлень ефіру за особистими режимами користувачів."""

import queue
import secrets
import sqlite3
import subprocess
import threading
import time

import requests

from core.diagnostics import runtime
from services.telegram_common import OGG_TIMEOUT_S, current_notification, delivery_trace
from services.telegram_media_store import remember_voice
from services.telegram_phrase_store import register_phrase
from services.telegram_retry import retry_delay
from services.user_notify_store import list_recipients

CONVERSION_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError)
QUEUE_SIZE = 32


class OutboxMixin:
    def _init_outbox(self):
        # Обмежена черга вихідних голосових сповіщень.
        self._outgoing_queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._outgoing_lock = threading.Lock()
        self._outgoing_thread = None
        self._outgoing_stop = threading.Event()

    # ---------- Отримувачі ----------

    def recipients_for(self, wants):
        """Активні користувачі, чий особистий режим приймає фразу.

        ``wants(mode)`` — чи підходить фраза під режим. Якщо прочитати права з
        бази не вдалося, не надсилаємо нікому.
        """
        try:
            users = list_recipients(self.db_path)
        except sqlite3.Error:
            runtime.count("telegram.recipients_unavailable")
            self.log.exception("Не вдалося прочитати отримувачів сповіщень; сповіщення пропущено")
            return []
        matches = {mode: bool(wants(mode)) for mode in ("all", "watch", "commands")}
        return [
            {**u, "matches": matches}
            for u in users if u["mode"] != "off" and wants(u["mode"])
        ]

    def _current_role(self, recipient):
        """Актуальна роль отримувача, якщо фраза досі йому належить, інакше None.

        Права й режим перевіряються під час надсилання, а не лише при постановці
        в чергу: пониження ролі чи зміна режиму діють і на накопичені сповіщення.
        """
        state = self._user_state(recipient["chat_id"])
        if state is None:
            return None
        role, mode = state
        matches = recipient.get("matches")
        if mode == "off" or (matches is not None and not matches.get(mode, False)):
            return None
        return role

    # ---------- Черга ----------

    def notify(self, watch_str, to_str, from_str, text, pcm, fix_id=None, recipients=()):
        """Повертає випадковий ID прийнятого сповіщення або None, якщо черга його не прийняла."""
        runtime.count("telegram.notify_requested")
        if not self.bot_token:
            runtime.count("telegram.notify_unconfigured")
            return
        if not recipients:
            runtime.count("telegram.notify_skipped_off")
            return

        lines = [f"Подія: {watch_str}"]
        if from_str or to_str:
            lines.append(f"Кому: {to_str or '-'} | Від: {from_str or '-'}")
        caption = "\n".join(lines) + f"\n\n{text}"

        with self._outgoing_lock:
            if self._outgoing_stop.is_set():
                runtime.count("telegram.notify_after_shutdown")
                self.log.warning("Telegram завершує роботу: сповіщення пропущено")
                return
            try:
                notification = secrets.token_hex(8)
                self._outgoing_queue.put_nowait((time.monotonic(), notification, (pcm, caption, fix_id, list(recipients))))
                runtime.count("telegram.enqueued")
                runtime.gauge("telegram.queue_depth", self._outgoing_queue.qsize())
            except queue.Full:
                runtime.count("telegram.queue_overflow")
                self.log.error("Чергу Telegram переповнено (%d): нове сповіщення пропущено", QUEUE_SIZE)
                return
            if self._outgoing_thread is None or not self._outgoing_thread.is_alive():
                self._outgoing_thread = threading.Thread(
                    target=self._outgoing_loop, name="telegram-outbox", daemon=True,
                )
                self._outgoing_thread.start()
            return notification

    def _outgoing_loop(self):
        """Надсилає повідомлення й завершується після спорожнення черги."""
        while True:
            if self._outgoing_stop.is_set() and self._outgoing_queue.empty():
                return
            try:
                queued_at, notification, item = self._outgoing_queue.get(timeout=0.2)
                queue_wait = time.monotonic() - queued_at
                runtime.observe("telegram.queue_wait", queue_wait)
                runtime.gauge("telegram.queue_depth", self._outgoing_queue.qsize())
            except queue.Empty:
                continue
            try:
                with delivery_trace(notification=notification, queue_wait_s=round(queue_wait, 3)):
                    self.send_voice(*item)
            except Exception:
                runtime.count("telegram.worker_error")
                self.log.exception("Помилка фонового надсилання Telegram")
            finally:
                self._outgoing_queue.task_done()

    def shutdown(self, timeout=10.0):
        """Припиняє прийом і чекає завершення надсилання."""
        with self._outgoing_lock:
            self._outgoing_stop.set()
            thread = self._outgoing_thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        pending = self._outgoing_queue.unfinished_tasks
        runtime.gauge("telegram.shutdown_pending", pending)
        if pending:
            self.log.warning(
                "Telegram: завершення з %d ненадісланими або незавершеними сповіщеннями", pending,
            )
            return False
        self.log.info("Чергу Telegram оброблено")
        return True

    # ---------- Голосові ----------

    def pcm_to_ogg(self, pcm: bytes) -> bytes:
        proc = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error",
                "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            ],
            input=pcm,
            capture_output=True,
            check=True,
            # Зависла конвертація не повинна зупинити єдиний потік надсилання.
            timeout=OGG_TIMEOUT_S,
        )
        return proc.stdout

    def send_voice(self, pcm: bytes, caption: str, fix_id, recipients):
        with delivery_trace(notification=current_notification() or secrets.token_hex(8)):
            self._send_voice(pcm, caption, fix_id, recipients)

    def _send_voice(self, pcm, caption, fix_id, recipients):
        active = [r for r in recipients if self._current_role(r) is not None]
        if not active:
            runtime.count("telegram.queue_discarded_off")
            return
        try:
            with runtime.timed("telegram.ogg_conversion"):
                ogg = self.pcm_to_ogg(pcm)
        except CONVERSION_ERRORS as e:
            runtime.count("telegram.conversion_failed")
            self.log.warning("Конвертація в OGG не вдалася (%s), надсилаю текстом", e)
            for index, recipient in enumerate(active, 1):
                role = self._current_role(recipient)
                if role is not None:
                    runtime.count("telegram.text_fallback")
                    with delivery_trace(recipient=index):
                        self.send_text(caption, self.keyboard_for(role, fix_id),
                                       parse_mode=None, chat_id=recipient["chat_id"])
            return

        duration = max(1, round(len(pcm) / (2 * self.sample_rate)))
        voice_ref = None  # file_id першого надісланого: далі без повторного завантаження
        for index, recipient in enumerate(active, 1):
            with delivery_trace(recipient=index, audio_bytes=len(ogg), audio_duration_s=duration):
                sent = self._deliver_voice(recipient, ogg, voice_ref, caption, duration, fix_id)
            if voice_ref is None and sent:
                voice_ref = sent

    def _deliver_voice(self, recipient, ogg, voice_ref, caption, duration, fix_id):
        """Надсилає голосове одному отримувачу. Повертає file_id або None."""
        chat_id = recipient["chat_id"]
        for attempt in range(1, 4):
            role = self._current_role(recipient)
            if role is None:
                return None
            reply_markup = self.keyboard_for(role, fix_id)
            data = {"chat_id": chat_id, "caption": caption[:1000],
                    "duration": duration, "reply_markup": reply_markup}
            kwargs = {"data": data, "timeout": (10, 60)}
            if voice_ref:
                data["voice"] = voice_ref
            else:
                kwargs["files"] = {"voice": ("phrase.ogg", ogg, "audio/ogg")}
            try:
                with delivery_trace(attempt=attempt):
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
                    if self._current_role(recipient) is not None:
                        self.send_text(caption, reply_markup, parse_mode=None, chat_id=chat_id)
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
                # file_id потрібен редактору Mini App, щоб відтворити запис фрази.
                remember_voice(self.db_path, fix_id, file_id)
            except sqlite3.Error:
                self.log.warning("Не вдалося зберегти зв’язок голосового з фразою")
            return file_id if isinstance(file_id, str) else None
        return None

    # ---------- Фрази для редактора ----------

    def register_phrase_for_fix(self, text: str, event_id: int | None) -> str | None:
        """Ідентифікатор фрази для редактора Mini App.

        ``db:<id>`` — подія SQLite; ``mem:<token>`` — тимчасова фраза на 24 години.
        None — фразу не вдалося зберегти: повідомлення матиме лише «Видалити».
        """
        if event_id is not None:
            return f"db:{event_id}"
        if not self.bot_token:
            return None
        token = secrets.token_hex(16)
        try:
            register_phrase(self.db_path, token, text, max_phrases=min(self.recent_phrases_max, 200))
        except (sqlite3.Error, ValueError) as exc:
            self.log.warning(
                "Не вдалося зберегти тимчасову фразу в SQLite: %s; виправлення в редакторі недоступне", exc,
            )
            return None
        return f"mem:{token}"
