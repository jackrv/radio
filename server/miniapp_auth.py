"""Проверка Telegram Mini App initData."""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    """Неверные или просроченные данные Telegram."""


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = 3600,
    now: int | None = None,
    return_user: bool = False,
) -> int | dict:
    """Проверяет initData и возвращает Telegram user.id."""

    if (
        not bot_token
        or not isinstance(init_data, str)
        or not (0 < len(init_data) <= 16384)
    ):
        raise TelegramAuthError(
            "Некоректні дані авторизації"
        )

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=64,
        )
    except (ValueError, UnicodeError) as exc:
        raise TelegramAuthError(
            "Некоректний формат initData"
        ) from exc

    fields = dict(pairs)

    if len(fields) != len(pairs):
        raise TelegramAuthError(
            "Повторювані поля initData"
        )

    given_hash = fields.pop("hash", "")

    if not re.fullmatch(
        r"[0-9a-fA-F]{64}", given_hash
    ):
        raise TelegramAuthError(
            "Відсутній коректний підпис"
        )

    check_string = "\n".join(
        f"{key}={fields[key]}"
        for key in sorted(fields)
    )

    secret = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    expected = hmac.new(
        secret,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected, given_hash.lower()
    ):
        raise TelegramAuthError(
            "Неправильний підпис Telegram"
        )

    auth_value = fields.get("auth_date", "")

    if not re.fullmatch(
        r"[0-9]{1,20}", auth_value
    ):
        raise TelegramAuthError(
            "Відсутня auth_date"
        )

    if max_age <= 0:
        raise ValueError(
            "max_age має бути додатним"
        )

    current_time = (
        int(time.time())
        if now is None
        else int(now)
    )

    issued_at = int(auth_value)

    if (
        issued_at > current_time + 60
        or current_time - issued_at > max_age
    ):
        raise TelegramAuthError(
            "Строк дії initData минув "
            "або дата неправильна"
        )

    try:
        user = json.loads(fields["user"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TelegramAuthError(
            "Відсутній коректний користувач"
        ) from exc

    user_id = (
        user.get("id")
        if isinstance(user, dict)
        else None
    )

    if type(user_id) is not int or user_id <= 0:
        raise TelegramAuthError(
            "Некоректний Telegram user.id"
        )

    return user if return_user else user_id
