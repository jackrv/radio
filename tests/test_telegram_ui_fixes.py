import time
from unittest.mock import Mock

import requests

from services.telegram import TelegramService
from services.user_store import bootstrap_admin, init_users_db
from tests.test_telegram_outbox import RECIPIENTS


def make_service(tmp_path):
    service = TelegramService(
        bot_token="fake_token",
        sample_rate=16000,
        db_path=str(tmp_path / "events.db"),
        log=Mock(),
    )
    init_users_db(service.db_path)
    bootstrap_admin(service.db_path, 1001)
    return service, {}


def test_default_text_keeps_html(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)

    service.send_text("<b>Проверка</b>", chat_id=1001)

    data = post.call_args.kwargs["data"]
    assert data["parse_mode"] == "HTML"


def test_conversion_failure_sends_plain_text(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    post = Mock()
    monkeypatch.setattr(requests, "post", post)

    service.pcm_to_ogg = Mock(
        side_effect=FileNotFoundError("ffmpeg")
    )

    service.send_voice(b"\x00" * 320, "А < Б & В", None, RECIPIENTS)

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

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    service.send_voice(b"\x00" * 320, "Сигнал < 10 & шум", None, RECIPIENTS)

    assert post.call_count == 4
    assert post.call_args.args[0].endswith("/sendMessage")
    assert "parse_mode" not in post.call_args.kwargs["data"]
