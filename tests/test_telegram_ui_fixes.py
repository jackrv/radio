import threading
from unittest.mock import Mock

import requests

import services.telegram as telegram_module
from services.telegram import TelegramService


def make_service(tmp_path):
    service = TelegramService(
        bot_token="fake_token",
        chat_id="123",
        sample_rate=16000,
        db_path=str(tmp_path / "events.db"),
        log=Mock(),
        state_lock=threading.Lock(),
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )
    return service, {}


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
