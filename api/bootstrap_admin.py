"""Первоначальная локальная настройка администратора."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
from services.user_store import (
    bootstrap_admin,
    init_users_db,
)


def resolve_private_admin_id(bot_token, chat_id, *, http_get=None):
    if not bot_token or not str(chat_id).isdigit():
        raise ValueError(
            "Потрібні токен бота та ID особистого чату Telegram"
        )

    getter = http_get or requests.get

    try:
        response = getter(
            f"https://api.telegram.org/bot{bot_token}/getChat",
            params={"chat_id": chat_id},
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Telegram не підтвердив чат")
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(
            "Не вдалося перевірити чат через Telegram"
        ) from None

    chat = payload.get("result") if payload.get("ok") else None

    if not isinstance(chat, dict):
        raise RuntimeError("Telegram не повернув відомості про чат")

    if chat.get("type") != "private":
        raise RuntimeError(
            "Зазначено не особистий чат. Автоматичну "
            "реєстрацію адміністратора заборонено."
        )

    user_id = chat.get("id")

    if (
        type(user_id) is not int
        or user_id <= 0
        or user_id != int(chat_id)
    ):
        raise RuntimeError("Некоректний Telegram ID")

    return user_id


def main():
    load_dotenv(config.BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    user_id = resolve_private_admin_id(token, chat_id)

    db_path = Path(config.DB_PATH)

    if not db_path.is_file():
        raise SystemExit("Робочу SQLite не знайдено")

    print("========== RADIO 345 ==========")
    print("База:", db_path)
    print("Telegram: особистий чат підтверджено")
    print("User ID:", user_id)
    print("Запропонована роль: admin")

    answer = input(
        "\nЦе ваш особистий обліковий запис? "
        "Для підтвердження введіть ТАК: "
    ).strip().casefold()

    if answer not in ("так", "да"):
        raise SystemExit("Реєстрацію скасовано")

    init_users_db(db_path)

    if bootstrap_admin(db_path, user_id):
        print("Першого адміністратора зареєстровано.")
    else:
        print("Цього адміністратора вже зареєстровано.")


if __name__ == "__main__":
    main()
