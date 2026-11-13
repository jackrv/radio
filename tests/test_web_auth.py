import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from services.miniapp_auth import (
    TelegramAuthError,
    verify_init_data,
)


TOKEN = "123456:example_TEST_token"
NOW = 1_800_000_000


def signed_data(
    *,
    user_id=123,
    auth_date=NOW,
    **extra,
):
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": "Тест",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        **extra,
    }

    check_string = "\n".join(
        f"{key}={fields[key]}"
        for key in sorted(fields)
    )

    secret = hmac.new(
        b"WebAppData",
        TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    fields["hash"] = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return urlencode(fields)


def test_accepts_valid_data():
    assert verify_init_data(
        signed_data(), TOKEN, now=NOW
    ) == 123


def test_accepts_signed_extra_field():
    assert verify_init_data(
        signed_data(signature="signed-extra"),
        TOKEN,
        now=NOW,
    ) == 123


def test_rejects_wrong_bot_token():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            signed_data(),
            "another-token",
            now=NOW,
        )


def test_rejects_modified_user():
    data = signed_data().replace(
        "%22id%22%3A123",
        "%22id%22%3A999",
    )

    with pytest.raises(TelegramAuthError):
        verify_init_data(
            data, TOKEN, now=NOW
        )


def test_rejects_expired_data():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            signed_data(auth_date=NOW - 3601),
            TOKEN,
            now=NOW,
        )


def test_rejects_future_data():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            signed_data(auth_date=NOW + 61),
            TOKEN,
            now=NOW,
        )


def test_rejects_duplicate_keys():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            signed_data() + "&auth_date=123",
            TOKEN,
            now=NOW,
        )


def test_rejects_missing_user():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            signed_data(user_id=None),
            TOKEN,
            now=NOW,
        )


def test_rejects_malformed_data():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            "not-a-query-string",
            TOKEN,
            now=NOW,
        )


def test_rejects_empty_data():
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            "", TOKEN, now=NOW
        )
