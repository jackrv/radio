import requests
from unittest.mock import Mock

import pytest

from services.telegram import TelegramService


def test_offset_survives_listener_restart(monkeypatch):
    service = TelegramService(
        bot_token="test_token",
        sample_rate=16000,
        db_path=":memory:",
        log=Mock(),
    )

    # Не вызываем настоящий обработчик команд.
    service.handle_update = Mock()

    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "result": [
            {"update_id": 41, "message": {"text": "test"}}
        ]
    }

    # Первый запрос успешен.
    # Затем имитируем остановку потока.
    # После перезапуска снова останавливаем поток.
    mock_get = Mock(
        side_effect=[
            response,
            KeyboardInterrupt(),
            KeyboardInterrupt(),
        ]
    )

    monkeypatch.setattr(
        requests,
        "get",
        mock_get,
    )

    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    # Перезапускаем тот же обработчик.
    with pytest.raises(KeyboardInterrupt):
        service.callback_listener()

    offsets = [
        call.kwargs["params"]["offset"]
        for call in mock_get.call_args_list
    ]

    assert offsets == [None, 42, 42]

    # Сообщение обработано только один раз.
    service.handle_update.assert_called_once()
