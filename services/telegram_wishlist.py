"""Приватні побажання адміністратора з Telegram, без виконання їхнього вмісту."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from services.private_files import private_open

_LOCK = threading.RLock()
WAIT_SECONDS = 24 * 3600
MAX_TEXT = 4096
_ADD = re.compile(r"^(?:ідея|идея|додати|добавить|/idea|/add)(?=$|[\s:])", re.IGNORECASE)
_CANCEL = {"/cancel", "скасувати", "отмена", "отменить"}


def _read(path):
    try:
        fd = private_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return stream.read()


def _replace(path, text):
    """Старий файл лишається цілим до атомарного rename; права тимчасового — 0600."""
    fd, temporary = tempfile.mkstemp(prefix=".local-wishlist-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class WishlistInbox:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / ".wishlist"
        self.directory = self.root / ".local-telegram-wishlist"
        self.state_path = self.directory / "state.json"

    @contextmanager
    def _locked(self):
        with _LOCK:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise OSError("Сховище побажань має бути звичайним каталогом")
            with os.fdopen(private_open(self.directory / "lock"), "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield

    def accept(self, actor, message_id, text, *, now=None):
        """Стан діалогу переживає рестарт; маркер у самому файлі захищає від повтору."""
        text = text.strip()
        match = _ADD.match(text)
        cancel = text.casefold() in _CANCEL
        if not match and not cancel and (text.startswith("/") or not self.directory.exists()):
            return None
        if type(message_id) is not int:
            raise ValueError("Немає ідентифікатора повідомлення")
        key = hashlib.sha256(f"{actor}:{message_id}".encode()).hexdigest()
        receipt = f"<!-- radio345-idea:{key} "
        now = time.time() if now is None else now
        user = str(actor)
        with self._locked():
            state = json.loads(_read(self.state_path) or "{}")
            if not isinstance(state, dict) or any(
                not isinstance(p, dict) or not isinstance(p.get("key"), str)
                or type(p.get("at")) not in (int, float) for p in state.values()
            ):
                raise ValueError("Пошкоджено стан введення ідеї")
            content = _read(self.path)
            if receipt in content:
                return "saved"
            # Неактуальні очікування прибираються, навіть якщо довго не було команд.
            state = {u: p for u, p in state.items() if 0 <= now - p["at"] < WAIT_SECONDS}
            pending = state.get(user)
            # Файл уже записано, але процес міг зникнути перед очищенням стану.
            if pending and f" session:{pending['key']} -->" in content:
                state.pop(user)
                pending = None
                _replace(self.state_path, json.dumps(state))
            if cancel:
                state.pop(user, None)
                _replace(self.state_path, json.dumps(state))
                return "cancelled"
            body = text[match.end():].lstrip(" :\n\t") if match else text
            if match and not body:
                state[user] = {"key": key, "at": now}
                _replace(self.state_path, json.dumps(state))
                return "waiting"
            if not match and pending is None:
                return None
            if not body or len(body) > MAX_TEXT or "\x00" in body:
                return "invalid"
            session = pending["key"] if pending else key
            stamp = datetime.fromtimestamp(now).astimezone().strftime("%d.%m.%Y %H:%M")
            entry = f"\n\n## Ідея · {stamp}\n\n{body}\n\n{receipt}session:{session} -->\n"
            _replace(self.path, content.rstrip() + entry)
            state.pop(user, None)
            try:
                _replace(self.state_path, json.dumps(state))
            except OSError:
                # Побажання вже на диску. Маркер session знешкодить старе очікування.
                pass
            return "saved"


class WishlistMixin:
    def _handle_wishlist_message(self, msg):
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        actor = self._private_sender(msg, msg.get("chat") or {})
        if actor is None:
            return False
        state = self._user_state(actor)
        if state is None or state[0] != "admin":
            return False
        # Події можуть бути в іншому каталозі в тестах/окремій інсталяції.
        inbox = WishlistInbox(Path(self.db_path).resolve().parent)
        try:
            result = inbox.accept(actor, msg.get("message_id"), text)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.log.warning("Не вдалося записати побажання Telegram (%s)", type(exc).__name__)
            self.send_text("⚠️ Не вдалося підтвердити запис ідеї. Повторіть повідомлення або перевірте доступ до файлу .wishlist.",
                           chat_id=actor, parse_mode=None)
            return True
        replies = {
            "waiting": "💡 Надішліть ідею наступним текстовим повідомленням — додам у .wishlist. Скасувати: /cancel. Очікування — до 24 годин.",
            "saved": "✅ Ідею збережено у .wishlist.",
            "cancelled": "Введення ідеї скасовано.",
            "invalid": "Надішліть непорожній текст до 4096 символів без нульових символів або /cancel.",
        }
        if result is None:
            return False
        # Не видаляємо вхідний текст: він лишається резервною копією в чаті.
        self.send_text(replies[result], chat_id=actor, parse_mode=None)
        return True
