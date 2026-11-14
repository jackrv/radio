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
import collections

from services.event_store import resolve_event_text, save_correction
from services.telegram_phrase_store import (
    register_phrase, load_phrase, update_phrase_text, RETENTION_SECONDS,
)
from services.sqlite_connection import get_db
from services.telegram_media_store import (
    remember_message, callback_fix_id, deletion_intent, deletion_result, FIX_RE,
)
from services.telegram_pending import (
    save_pending_correction,
    load_pending_correction,
    cancel_pending_correction,
    apply_pending_correction,
)
from services.telegram_retry import retry_delay, listener_retry_delay
from services.telegram_offset_store import (
    load_telegram_offset,
    save_telegram_offset,
)

class TelegramService:
    def __init__(
    self,
    bot_token,
    chat_id,
    sample_rate,
    db_path,
    log,
    state_lock,
    waiting_for_correction,
    get_notify_mode,
    set_notify_mode,
    apply_telegram_mode=None,
):
        self.recent_phrases = collections.OrderedDict()
        self._phrase_created_at = {}
        self._phrases_lock = threading.Lock()
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
        self.waiting_for_correction = waiting_for_correction
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
        row = [{"text": "🗑 Удалить", "callback_data": delete_data}]
        rows = [row]
        if fix_id is not None:
            if (
                fix_id.startswith("db:")
                or re.fullmatch(r"mem:[0-9a-f]{32}", fix_id)
            ):
                # Web App buttons are supported in private Telegram chats.
                rows.append([{
                    "text": "✏️ Исправить в редакторе",
                    "web_app": {
                        "url": "https://jackrv.github.io/radio/?fix_id="
                        + quote(fix_id, safe=""),
                    },
                }])
                rows.append([{
                    "text": "💬 Исправить сообщением",
                    "callback_data": f"fix_{fix_id}",
                }])
            else:
                # Legacy/in-memory fallback when persistent storage is unavailable.
                row.append({
                    "text": "✏️ Исправить",
                    "callback_data": f"fix_{fix_id}",
                })
        return json.dumps({"inline_keyboard": rows})

    def send_text(self, text: str, reply_markup: str | None = None, parse_mode: str | None = "HTML"):
        if not (self.bot_token and self.chat_id):
            return

        data = {
            "chat_id": self.chat_id,
            "text": text[:4000],
        }
        if parse_mode is not None:
            data["parse_mode"] = parse_mode

        if reply_markup:
            data["reply_markup"] = reply_markup

        try:
            r = requests.post(
                f"{self.api}/sendMessage",
                data=data,
                timeout=15,
            )
            r.raise_for_status()
            if reply_markup:
                try:
                    markup = json.loads(reply_markup)
                    for row in markup.get("inline_keyboard", []):
                        for button in row:
                            data = button.get("callback_data", "")
                            if data.startswith("del:"):
                                self._remember_sent(r, data[4:])
                except (TypeError, ValueError):
                    pass
        except requests.RequestException as e:
            self.log.error("Telegram (текст) не отправлен: %s", e)

    def _remember_sent(self, response, fix_id):
        if not fix_id:
            return
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("ok") is True:
                remember_message(self.db_path, fix_id, payload.get("result"))
        except (sqlite3.Error, ValueError):
            # Do not retry an already sent message when local metadata fails.
            self.log.warning("Не удалось сохранить связь сообщения Telegram с фразой")

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

    def send_voice(self, pcm: bytes, caption: str, fix_id=None):
        if not self._notifications_enabled():
            return

        reply_markup = self.get_keyboard(fix_id)

        try:
            ogg = self.pcm_to_ogg(pcm)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.log.warning(
                "Конвертация в OGG не удалась (%s), отправляю текстом", e
            )
            if self._notifications_enabled():
                self.send_text(caption, reply_markup, parse_mode=None)
            return

        duration = max(1, round(len(pcm) / (2 * self.sample_rate)))

        for attempt in range(1, 4):
            if not self._notifications_enabled():
                return

            try:
                r = requests.post(
                    f"{self.api}/sendVoice",
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
                    "Попытка %d/3 отправить голосовое не удалась: %s",
                    attempt, e,
                )
                if attempt == 3:
                    self.log.error(
                        "Все попытки исчерпаны, отправляю резервным текстом"
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

    def _outgoing_loop(self):
        """Отправляет сообщения и завершается после опустошения очереди."""
        while True:
            if (
                self._outgoing_stop.is_set()
                and self._outgoing_queue.empty()
            ):
                return

            try:
                item = self._outgoing_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                if self._notifications_enabled():
                    self.send_voice(*item)
                else:
                    self.log.info(
                        "Уведомление из очереди отброшено: режим OFF"
                    )
            except Exception:
                self.log.exception(
                    "Ошибка фоновой отправки Telegram"
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

        if pending:
            self.log.warning(
                "Telegram: завершение с %d неотправленными "
                "или незавершёнными уведомлениями",
                pending,
            )
            return False

        self.log.info("Очередь Telegram обработана")
        return True

    def notify(self, watch_str, to_str, from_str, text, pcm, fix_id=None):
        if not (self.bot_token and self.chat_id):
            return

        if not self._notifications_enabled():
            return

        lines = [f"Событие: {watch_str}"]
        if from_str or to_str:
            lines.append(f"Кому: {to_str or '-'} | От: {from_str or '-'}")

        caption = "\n".join(lines) + f"\n\n{text}"

        with self._outgoing_lock:
            if self._outgoing_stop.is_set():
                self.log.warning(
                    "Telegram завершает работу: уведомление пропущено"
                )
                return

            try:
                self._outgoing_queue.put_nowait(
                    (pcm, caption, fix_id)
                )
            except queue.Full:
                self.log.error(
                    "Очередь Telegram переполнена (32): "
                    "новое уведомление пропущено"
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

    def register_phrase_for_fix(self, text: str, event_id: int | None) -> str:
        if event_id is not None:
            return f"db:{event_id}"

        token = secrets.token_hex(16)
        if self.bot_token and self.chat_id:
            try:
                register_phrase(
                    self.db_path, token, text,
                    max_phrases=min(self.recent_phrases_max, 200),
                )
            except (sqlite3.Error, ValueError) as exc:
                self.log.warning(
                    "Не удалось сохранить временную фразу в SQLite: %s; "
                    "используется исправление через чат", exc,
                )
                token = secrets.token_hex(8)
        else:
            # Without Telegram credentials no notifications can be sent.
            token = secrets.token_hex(8)

        with self._phrases_lock:
            now = time.time()
            for old_token, created_at in list(self._phrase_created_at.items()):
                if created_at <= now - RETENTION_SECONDS:
                    self.recent_phrases.pop(old_token, None)
                    del self._phrase_created_at[old_token]
            self.recent_phrases[token] = text
            self._phrase_created_at[token] = now
            while len(self.recent_phrases) > min(self.recent_phrases_max, 200):
                old_token, _ = self.recent_phrases.popitem(last=False)
                self._phrase_created_at.pop(old_token, None)

        return f"mem:{token}"

    def resolve_phrase_by_fix_id(self, fix_id: str) -> tuple[str | None, int | None]:
        if fix_id.startswith("db:"):
            ev_id = int(fix_id.split(":", 1)[1])
            text = resolve_event_text(ev_id, self.db_path)
            return (text, ev_id) if text is not None else (None, None)

        if fix_id.startswith("mem:"):
            token = fix_id.split(":", 1)[1]
            if re.fullmatch(r"[0-9a-f]{32}", token):
                try:
                    phrase = load_phrase(self.db_path, token)
                    return (
                        (phrase["original_text"], None)
                        if phrase is not None else (None, None)
                    )
                except sqlite3.Error as exc:
                    self.log.warning("Не удалось прочитать фразу: %s", exc)
                    return None, None
            with self._phrases_lock:
                created_at = self._phrase_created_at.get(token)
                if (
                    created_at is not None
                    and created_at <= time.time() - RETENTION_SECONDS
                ):
                    self.recent_phrases.pop(token, None)
                    self._phrase_created_at.pop(token, None)
                    return None, None
                return self.recent_phrases.get(token), None

        return None, None

    def handle_update(self, upd):
        cq = upd.get("callback_query")

        if cq:
            chat_id = str(
                ((cq.get("message") or {}).get("chat") or {}).get("id", "")
            )
            if chat_id != str(self.chat_id):
                return

            data = cq.get("data", "")
            msg_id = (cq.get("message") or {}).get("message_id")

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
                        "text": "Удалено" if success else "Ошибка",
                    },
                    timeout=15,
                )

            elif data == "cancel_fix":
                # Сначала очищаем сохранённое ожидание.
                if type(upd.get("update_id")) is int:
                    cancel_pending_correction(
                        self.db_path, chat_id
                    )

                # Затем очищаем оперативную память.
                with self.state_lock:
                    self.waiting_for_correction.pop(chat_id, None)

                try:
                    requests.post(
                        f"{self.api}/answerCallbackQuery",
                        data={
                            "callback_query_id": cq["id"],
                            "text": "Исправление отменено",
                        },
                        timeout=15,
                    )
                except requests.RequestException as e:
                    self.log.warning(
                        "Не удалось подтвердить отмену: %s", e
                    )

                if msg_id is not None:
                    try:
                        requests.post(
                            f"{self.api}/deleteMessage",
                            data={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                            },
                            timeout=15,
                        )
                    except requests.RequestException as e:
                        self.log.warning(
                            "Не удалось удалить сообщение отмены: %s",
                            e,
                        )

            elif data.startswith("fix_"):
                fix_id = data.split("_", 1)[1]
                orig_text, ev_id = self.resolve_phrase_by_fix_id(fix_id)

                if not orig_text:
                    requests.post(
                        f"{self.api}/answerCallbackQuery",
                        data={
                            "callback_query_id": cq["id"],
                            "text": "Фраза устарела",
                        },
                        timeout=15,
                    )
                    return

                pending = {
                    "event_id": ev_id,
                    "raw_text": orig_text,
                    "source_id": (
                        fix_id if ev_id is None else None
                    ),
                }

                if type(upd.get("update_id")) is int:
                    save_pending_correction(
                        self.db_path, chat_id, pending
                    )

                with self.state_lock:
                    self.waiting_for_correction[chat_id] = pending

                requests.post(
                    f"{self.api}/answerCallbackQuery",
                    data={
                        "callback_query_id": cq["id"],
                        "text": "Отправьте правильный текст",
                    },
                    timeout=15,
                )

                safe_orig_text = html.escape(orig_text)

                keyboard = []
                if len(orig_text) <= 256:
                    keyboard.append({
                        "text": "📋 Скопировать текст",
                        "copy_text": {"text": orig_text},
                    })

                keyboard.append({
                    "text": "❌ Отмена",
                    "callback_data": "cancel_fix",
                })

                cancel_markup = json.dumps(
                    {"inline_keyboard": [keyboard]},
                    ensure_ascii=False,
                )

                copy_hint = (
                    "Нажмите «Скопировать текст», затем вставьте его "
                    "в строку ввода и отправьте."
                    if len(orig_text) <= 256
                    else "Скопируйте исходный текст из сообщения вручную, "
                         "отредактируйте и отправьте."
                )

                self.send_text(
                    f"✍️ <b>Режим исправления</b>\n"
                    f"Исходный текст: <code>{safe_orig_text}</code>\n\n"
                    f"{copy_hint}",
                    cancel_markup,
                )

        msg = upd.get("message")
        if not msg:
            return

        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(self.chat_id):
            return

        text_msg = (msg.get("text") or "").strip().lower()

        modes = {
            "/all": ("all", "📢 Режим уведомлений изменен: <b>ALL (Все фразы)</b>"),
            "all": ("all", "📢 Режим уведомлений изменен: <b>ALL (Все фразы)</b>"),
            "все": ("all", "📢 Режим уведомлений изменен: <b>ALL (Все фразы)</b>"),
            "/watch": ("watch", "👀 Режим уведомлений изменен: <b>WATCH (Только вачлист)</b>"),
            "watch": ("watch", "👀 Режим уведомлений изменен: <b>WATCH (Только вачлист)</b>"),
            "вач": ("watch", "👀 Режим уведомлений изменен: <b>WATCH (Только вачлист)</b>"),
            "/commands": ("commands", "⚡️ Режим уведомлений изменен: <b>COMMANDS (Команды и позывные)</b>"),
            "commands": ("commands", "⚡️ Режим уведомлений изменен: <b>COMMANDS (Команды и позывные)</b>"),
            "команды": ("commands", "⚡️ Режим уведомлений изменен: <b>COMMANDS (Команды и позывные)</b>"),
            "cmd": ("commands", "⚡️ Режим уведомлений изменен: <b>COMMANDS (Команды и позывные)</b>"),
            "/off": ("off", "🔕 Автоматические уведомления отключены."),
            "off": ("off", "🔕 Автоматические уведомления отключены."),
            "/silent": ("off", "🔕 Автоматические уведомления отключены."),
            "тихо": ("off", "🔕 Автоматические уведомления отключены."),
        }

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
                    "Повторная Telegram-команда %s пропущена",
                    update_id,
                )
                return

            self.log.info("Режим изменен на: %s", mode)
            self.send_text(response)
            return

        corrected_text = (msg.get("text") or "").strip()
        if not corrected_text:
            return

        with self.state_lock:
            pending = self.waiting_for_correction.get(chat_id)

        if (
            pending is None
            and type(upd.get("update_id")) is int
        ):
            pending = load_pending_correction(
                self.db_path, chat_id
            )

        if pending:
            orig_text = pending["raw_text"]
            ev_id = pending["event_id"]

            options = {}
            if pending.get("source_id") is not None:
                options["source_id"] = pending["source_id"]

            if type(upd.get("update_id")) is int:
                stored = apply_pending_correction(
                    self.db_path,
                    chat_id,
                    corrected_text,
                    upd["update_id"],
                )

                if stored is None:
                    with self.state_lock:
                        self.waiting_for_correction.pop(
                            chat_id, None
                        )
                    return

                orig_text = stored["raw_text"]
            else:
                # Совместимость со старыми прямыми вызовами
                # handle_update без Telegram update_id.
                source = pending.get("source_id", "") or ""
                if re.fullmatch(r"mem:[0-9a-f]{32}", source):
                    with get_db(self.db_path) as db:
                        db.execute("BEGIN IMMEDIATE")
                        save_correction(
                            orig_text, corrected_text, ev_id, self.db_path,
                            db=db, **options,
                        )
                        update_phrase_text(
                            db, source[4:], corrected_text, allow_missing=True,
                        )
                else:
                    save_correction(
                        orig_text, corrected_text, ev_id, self.db_path,
                        **options,
                    )

            # Удаляем ожидание только после успешного сохранения.
            with self.state_lock:
                if self.waiting_for_correction.get(chat_id) is pending:
                    self.waiting_for_correction.pop(chat_id, None)

            self.log.info(
                "Обучение сохранено: '%s' -> '%s'",
                orig_text,
                corrected_text,
            )

            self.send_text(
                f"✅ <b>Успешно обучено!</b>\n"
                f"Было: <code>{html.escape(orig_text)}</code>\n"
                f"Стало: <code>{html.escape(corrected_text)}</code>"
            )

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
                r = requests.get(
                    f"{self.api}/getUpdates",
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
                        "повтор через %s сек. (ошибок подряд: %s)",
                        r.status_code, delay, consecutive_errors,
                    )
                    time.sleep(delay)
                    continue

                if consecutive_errors:
                    self.log.info(
                        "Связь с Telegram восстановлена "
                        "после %s ошибок опроса",
                        consecutive_errors,
                    )
                    consecutive_errors = 0

                for upd in r.json().get("result", []):
                    try:
                        update_id = upd["update_id"]
                        if type(update_id) is not int:
                            raise TypeError("update_id должен быть целым числом")
                    except (KeyError, TypeError):
                        self.log.exception(
                            "Telegram прислал update без корректного update_id"
                        )
                        continue

                    # Защита от повторного получения уже
                    # подтверждённого обновления.
                    if (
                        self._updates_offset is not None
                        and update_id < self._updates_offset
                    ):
                        continue

                    try:
                        self.handle_update(upd)

                    except Exception:
                        retries = getattr(self, "_update_retries", None)

                        if retries is None:
                            retries = {}
                            self._update_retries = retries

                        attempts = retries.get(update_id, 0) + 1
                        retries[update_id] = attempts

                        self.log.exception(
                            "Ошибка Telegram update_id=%s, попытка %s/3",
                            update_id,
                            attempts,
                        )

                        if attempts < 3:
                            time.sleep(5)
                            break

                        try:
                            self._quarantine_update(upd)
                        except Exception:
                            self.log.exception(
                                "Не удалось сохранить проблемный Telegram update. "
                                "Offset не изменён."
                            )
                            time.sleep(5)
                            break

                        self._advance_offset(update_id)
                        retries.pop(update_id, None)

                        self.log.critical(
                            "Telegram update_id=%s сохранён в "
                            "telegram_failed_updates.jsonl. "
                            "Требуется ручная проверка.",
                            update_id,
                        )

                    else:
                        self._advance_offset(update_id)
                        retries = getattr(self, "_update_retries", None)

                        if retries is not None:
                            retries.pop(update_id, None)

            except (requests.RequestException, ValueError, KeyError) as e:
                consecutive_errors += 1
                delay = listener_retry_delay(consecutive_errors)

                self.log.warning(
                    "Сбой связи в callback_listener: %s; "
                    "повтор через %s сек. (ошибок подряд: %s)",
                    e, delay, consecutive_errors,
                )
                time.sleep(delay)
