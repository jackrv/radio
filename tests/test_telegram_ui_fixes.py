import json
import threading
from unittest.mock import Mock

import requests

import services.telegram as telegram_module
from services.telegram import TelegramService


def make_service(tmp_path):
    pending = {}
    service = TelegramService(
        bot_token="fake_token",
        chat_id="123",
        sample_rate=16000,
        db_path=str(tmp_path / "events.db"),
        log=Mock(),
        state_lock=threading.Lock(),
        waiting_for_correction=pending,
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )
    return service, pending


def test_cancel_clears_pending_state(tmp_path, monkeypatch):
    service, pending = make_service(tmp_path)
    pending["123"] = {
        "event_id": 10,
        "raw_text": "Ліра 68",
    }

    post = Mock()
    monkeypatch.setattr(telegram_module.requests, "post", post)

    service.handle_update({
        "callback_query": {
            "id": "callback_1",
            "data": "cancel_fix",
            "message": {
                "chat": {"id": 123},
                "message_id": 55,
            },
        }
    })

    assert "123" not in pending
    urls = [call.args[0] for call in post.call_args_list]
    assert any(url.endswith("/answerCallbackQuery") for url in urls)
    assert any(url.endswith("/deleteMessage") for url in urls)


def test_default_text_keeps_html(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    post = Mock()
    monkeypatch.setattr(telegram_module.requests, "post", post)

    service.send_text("<b>Проверка</b>")

    data = post.call_args.kwargs["data"]
    assert data["parse_mode"] == "HTML"


def test_conversion_failure_sends_plain_text(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    post = Mock()
    monkeypatch.setattr(telegram_module.requests, "post", post)

    service.pcm_to_ogg = Mock(
        side_effect=FileNotFoundError("ffmpeg")
    )

    service.send_voice(b"\x00" * 320, "А < Б & В")

    assert post.call_args.args[0].endswith("/sendMessage")
    data = post.call_args.kwargs["data"]
    assert data["text"] == "А < Б & В"
    assert "parse_mode" not in data


def test_failed_voice_retries_use_plain_text(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    service.pcm_to_ogg = Mock(return_value=b"ogg")

    post = Mock(side_effect=[
        requests.Timeout("timeout"),
        requests.Timeout("timeout"),
        requests.Timeout("timeout"),
        Mock(),
    ])

    monkeypatch.setattr(telegram_module.requests, "post", post)
    monkeypatch.setattr(telegram_module.time, "sleep", lambda _: None)

    service.send_voice(b"\x00" * 320, "Сигнал < 10 & шум")

    assert post.call_count == 4
    assert post.call_args.args[0].endswith("/sendMessage")
    assert "parse_mode" not in post.call_args.kwargs["data"]


def test_correction_prompt_has_real_newlines(tmp_path, monkeypatch):
    service, pending = make_service(tmp_path)

    service.send_text = Mock()
    monkeypatch.setattr(
        telegram_module.requests, "post", Mock()
    )

    fix_id = service.register_phrase_for_fix("Ліра <68>", None)

    service.handle_update({
        "callback_query": {
            "id": "callback_2",
            "data": f"fix_{fix_id}",
            "message": {
                "chat": {"id": 123},
                "message_id": 56,
            },
        }
    })

    message = service.send_text.call_args.args[0]
    keyboard = json.loads(service.send_text.call_args.args[1])

    assert "\n\n" in message
    assert "\\n" not in message
    assert "&lt;68&gt;" in message
    assert pending["123"]["raw_text"] == "Ліра <68>"
    assert any(
        button.get("callback_data") == "cancel_fix"
        for row in keyboard["inline_keyboard"]
        for button in row
    )


def test_success_message_has_real_newlines(tmp_path, monkeypatch):
    service, pending = make_service(tmp_path)
    pending["123"] = {
        "event_id": None,
        "raw_text": "Ліра 68",
    }

    service.send_text = Mock()
    save = Mock()

    monkeypatch.setattr(telegram_module, "save_correction", save)

    service.handle_update({
        "message": {
            "chat": {"id": 123},
            "text": "Ліра 68, прийом",
        }
    })

    message = service.send_text.call_args.args[0]

    assert message.count("\n") >= 2
    assert "\\n" not in message
    assert "123" not in pending
    save.assert_called_once()
