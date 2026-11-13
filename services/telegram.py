import json
import queue
import html
import logging
import subprocess
import threading
import time
import uuid

import requests
import collections

from services.event_store import resolve_event_text, save_correction
from services.telegram_retry import retry_delay

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
):
        self.recent_phrases = collections.OrderedDict()
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
        self._updates_offset = None

        # Ограниченная очередь исходящих голосовых уведомлений.
        self._outgoing_queue = queue.Queue(maxsize=32)
        self._outgoing_lock = threading.Lock()
        self._outgoing_thread = None
        self._outgoing_stop = threading.Event()

    def get_keyboard(self, fix_id=None):
        """Кнопки управления сообщением."""
        row = [{"text": "🗑 Удалить", "callback_data": "del"}]
        if fix_id is not None:
            row.append({
                "text": "✏️ Исправить",
                "callback_data": f"fix_{fix_id}",
            })
        return json.dumps({"inline_keyboard": [row]})

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
        except requests.RequestException as e:
            self.log.error("Telegram (текст) не отправлен: %s", e)

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

    def send_voice(self, pcm: bytes, caption: str, fix_id=None):
        reply_markup = self.get_keyboard(fix_id)

        try:
            ogg = self.pcm_to_ogg(pcm)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.log.warning(
                "Конвертация в OGG не удалась (%s), отправляю текстом", e
            )
            self.send_text(caption, reply_markup, parse_mode=None)
            return

        duration = max(1, round(len(pcm) / (2 * self.sample_rate)))

        for attempt in range(1, 4):
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
                    self.send_text(caption, reply_markup, parse_mode=None)
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
                self.send_voice(*item)
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

        token = uuid.uuid4().hex[:8]
        with self._phrases_lock:
            self.recent_phrases[token] = text

            while len(self.recent_phrases) > self.recent_phrases_max:
                self.recent_phrases.popitem(last=False)

        return f"mem:{token}"

    def resolve_phrase_by_fix_id(self, fix_id: str) -> tuple[str | None, int | None]:
        if fix_id.startswith("db:"):
            ev_id = int(fix_id.split(":", 1)[1])
            text = resolve_event_text(ev_id, self.db_path)
            return (text, ev_id) if text is not None else (None, None)

        if fix_id.startswith("mem:"):
            token = fix_id.split(":", 1)[1]
            with self._phrases_lock:
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

            if data == "del":
                d = requests.post(
                    f"{self.api}/deleteMessage",
                    data={"chat_id": chat_id, "message_id": msg_id},
                    timeout=15,
                ).json()

                requests.post(
                    f"{self.api}/answerCallbackQuery",
                    data={
                        "callback_query_id": cq["id"],
                        "text": "Удалено" if d.get("ok") else "Ошибка",
                    },
                    timeout=15,
                )

            elif data == "cancel_fix":
                # Сначала отменяем режим, независимо от состояния сети.
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

                with self.state_lock:
                    self.waiting_for_correction[chat_id] = {
                        "event_id": ev_id,
                        "raw_text": orig_text,
                    }

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
        }

        if text_msg in modes:
            mode, response = modes[text_msg]
            with self.state_lock:
                self.set_notify_mode(mode)

            self.log.info("Режим изменен на: %s", mode)
            self.send_text(response)
            return

        corrected_text = (msg.get("text") or "").strip()
        if not corrected_text:
            return

        with self.state_lock:
            pending = self.waiting_for_correction.get(chat_id)

        if pending:
            orig_text = pending["raw_text"]
            ev_id = pending["event_id"]

            save_correction(
                orig_text,
                corrected_text,
                ev_id,
                self.db_path,
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

    def callback_listener(self):
        """Слушает нажатия кнопок и команды управления режимами/обучением."""

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
                    self.log.warning(
                        "Telegram getUpdates вернул статус %s", r.status_code
                    )
                    time.sleep(5)
                    continue

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

                        self._updates_offset = update_id + 1
                        retries.pop(update_id, None)

                        self.log.critical(
                            "Telegram update_id=%s сохранён в "
                            "telegram_failed_updates.jsonl. "
                            "Требуется ручная проверка.",
                            update_id,
                        )

                    else:
                        self._updates_offset = update_id + 1
                        retries = getattr(self, "_update_retries", None)

                        if retries is not None:
                            retries.pop(update_id, None)

            except (requests.RequestException, ValueError, KeyError) as e:
                self.log.warning("Сбой связи в callback_listener: %s", e)
                time.sleep(5)