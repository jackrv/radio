from unittest.mock import Mock

import pytest

from api.bootstrap_admin import resolve_private_admin_id


def fake_get(chat_type="private", chat_id=12345):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "ok": True,
        "result": {
            "id": chat_id,
            "type": chat_type,
        },
    }
    return Mock(return_value=response)


def test_accepts_private_chat():
    getter = fake_get()

    assert resolve_private_admin_id(
        "test_token",
        "12345",
        http_get=getter,
    ) == 12345

    getter.assert_called_once()


def test_rejects_group_chat():
    with pytest.raises(RuntimeError):
        resolve_private_admin_id(
            "test_token",
            "12345",
            http_get=fake_get("group"),
        )


def test_rejects_mismatched_chat():
    with pytest.raises(RuntimeError):
        resolve_private_admin_id(
            "test_token",
            "12345",
            http_get=fake_get(chat_id=67890),
        )


def test_rejects_invalid_chat_id():
    with pytest.raises(ValueError):
        resolve_private_admin_id(
            "test_token",
            "-10012345",
            http_get=fake_get(),
        )
