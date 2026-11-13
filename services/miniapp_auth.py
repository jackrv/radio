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
) -> int:
    """Проверяет initData и возвращает Telegram user.id."""

    if (
        not bot_token
        or not isinstance(init_data, str)
        or not (0 < len(init_data) <= 16384)
    ):
        raise TelegramAuthError(
            "Некорректные данные авторизации"
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
            "Некорректный формат initData"
        ) from exc

    fields = dict(pairs)

    if len(fields) != len(pairs):
        raise TelegramAuthError(
            "Повторяющиеся поля initData"
        )

    given_hash = fields.pop("hash", "")

    if not re.fullmatch(
        r"[0-9a-fA-F]{64}", given_hash
    ):
        raise TelegramAuthError(
            "Отсутствует корректная подпись"
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
            "Неверная подпись Telegram"
        )

    auth_value = fields.get("auth_date", "")

    if not re.fullmatch(
        r"[0-9]{1,20}", auth_value
    ):
        raise TelegramAuthError(
            "Отсутствует auth_date"
        )

    if max_age <= 0:
        raise ValueError(
            "max_age должен быть положительным"
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
            "Срок действия initData истёк "
            "или дата неверна"
        )

    try:
        user = json.loads(fields["user"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TelegramAuthError(
            "Отсутствует корректный пользователь"
        ) from exc

    user_id = (
        user.get("id")
        if isinstance(user, dict)
        else None
    )

    if type(user_id) is not int or user_id <= 0:
        raise TelegramAuthError(
            "Некорректный Telegram user.id"
        )

    return user_id
