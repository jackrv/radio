"""Реєстрація першого адміністратора з TELEGRAM_CHAT_ID у .env.

Лише стандартна бібліотека: модуль імпортує лаунчер. Telegram ID беремо
з особистого чату, підтвердженого через getChat; ім’я та username — з
тієї ж відповіді, тож вручну шукати їх не потрібно.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from services.dictionary_setup import has_active_admin
from services.user_profiles import remember_verified_profile
from services.user_store import bootstrap_admin, init_users_db

YES_ANSWERS = {"y", "yes"}


def parse_private_chat(payload, chat_id):
    """Перевіряє відповідь getChat і повертає дані особистого чату."""
    chat = payload.get("result") if isinstance(payload, dict) and payload.get("ok") else None
    if not isinstance(chat, dict):
        raise RuntimeError("Telegram не повернув відомості про чат")
    if chat.get("type") != "private":
        raise RuntimeError(
            "TELEGRAM_CHAT_ID — не особистий чат. Автоматичну "
            "реєстрацію адміністратора заборонено."
        )
    user_id = chat.get("id")
    if type(user_id) is not int or user_id <= 0 or user_id != int(chat_id):
        raise RuntimeError("Некоректний Telegram ID")
    return chat


def validate_settings(bot_token, chat_id):
    if not bot_token or not str(chat_id).isdigit():
        raise ValueError(
            "Потрібні TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID особистого чату в .env"
        )


def fetch_private_chat(bot_token, chat_id, *, urlopen=urllib.request.urlopen):
    """Запит getChat; текст помилок не містить токена."""
    validate_settings(bot_token, chat_id)
    url = (
        f"https://api.telegram.org/bot{bot_token}/getChat?"
        + urllib.parse.urlencode({"chat_id": chat_id})
    )
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram не підтвердив чат (HTTP {exc.code})") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Не вдалося перевірити чат через Telegram") from None
    return parse_private_chat(payload, chat_id)


def display_name(chat):
    return " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])).strip()


def _user_count(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15.0)) as db:
            return db.execute("SELECT COUNT(*) FROM telegram_users").fetchone()[0]
    except sqlite3.Error:
        return 0


def register_admin(db_path, chat):
    """Записує адміністратора й профіль. Повертає False, якщо його вже зареєстровано."""
    init_users_db(db_path)
    created = bootstrap_admin(db_path, chat["id"])
    remember_verified_profile(db_path, chat)
    return created


def ensure_admin(
    db_path,
    bot_token,
    chat_id,
    command_hint: str,
    *,
    interactive: bool,
    has_admin=has_active_admin,
    ask=input,
    out=print,
    fetch=fetch_private_chat,
) -> bool:
    """Повертає True, якщо активний адміністратор є або щойно зареєстрований."""
    if has_admin(db_path):
        return True

    out("⚠️ У базі немає адміністратора Mini App — без нього API не запуститься.")
    if _user_count(db_path):
        out("  У базі вже є користувачі, тож автоматична реєстрація заборонена.")
        out("  Увімкніть або призначте адміністратора серед наявних користувачів.")
        return False

    try:
        chat = fetch(bot_token, chat_id)
    except (ValueError, RuntimeError) as exc:
        out(f"  {exc}")
        return False

    name = display_name(chat)
    out("  Кандидат із TELEGRAM_CHAT_ID у .env (особистий чат підтверджено Telegram):")
    out(f"    Ім’я:        {name or '—'}")
    if chat.get("username"):
        out(f"    Username:    @{chat['username']}")
    out(f"    Telegram ID: {chat['id']}")

    if not interactive:
        out(f"  Консоль неінтерактивна. Зареєструйте вручну: {command_hint}")
        return False

    try:
        answer = ask("  Зареєструвати його адміністратором? [Y/N]: ")
    except (EOFError, KeyboardInterrupt):
        out("")
        answer = ""
    if answer.strip().lower() not in YES_ANSWERS:
        out("  Реєстрацію скасовано. Запуск зупинено.")
        return False

    try:
        register_admin(db_path, chat)
    except (sqlite3.Error, RuntimeError) as exc:
        out(f"Помилка: адміністратора не зареєстровано ({type(exc).__name__}).")
        return False
    out(f"✅ Адміністратора зареєстровано: {name or chat['id']}.")
    return True
