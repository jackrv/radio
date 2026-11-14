"""Зв’язок черги, повторів і резервного тексту; мережа й аудіо лише синтетичні."""

import logging
import re
from unittest.mock import Mock

import requests

from core.diagnostics import RuntimeDiagnostics
import services.telegram_common as common
import services.telegram_outbox as outbox
from tests.test_telegram_outbox import RECIPIENTS, make_service


def response(*, voice=False):
    result = {"voice": {"file_id": "PRIVATE_FILE_ID"}} if voice else {}
    reply = Mock(status_code=200)
    reply.json.return_value = {"ok": True, "result": result}
    return reply


def records(caplog):
    return [dict(field.split("=", 1) for field in record.getMessage().split()[1:])
            for record in caplog.records if record.name == "radio.diagnostics"]


def test_retries_share_notification_but_attempts_and_recipients_differ(monkeypatch, caplog):
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(common, "runtime", metrics)
    monkeypatch.setattr(outbox, "runtime", metrics)
    service = make_service()
    service.pcm_to_ogg = Mock(return_value=b"OggS")
    sleep = Mock()
    monkeypatch.setattr("time.sleep", sleep)
    post = Mock(side_effect=[requests.ReadTimeout("PRIVATE_ERROR"), response(voice=True), response(voice=True)])
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", Mock(return_value=response()))
    recipients = RECIPIENTS + [{"chat_id": 987654321, "role": "admin", "mode": "all"}]

    with caplog.at_level(logging.INFO, logger="radio.diagnostics"):
        service.send_voice(b"\0\0", "PRIVATE_PHRASE", None, recipients)
        service._request("get", "getUpdates")

    voice, polling = records(caplog)[:3], records(caplog)[3]
    assert len({row["notification"] for row in voice}) == 1
    assert re.fullmatch(r"[0-9a-f]{16}", voice[0]["notification"])
    assert [(r["recipient"], r["attempt"]) for r in voice] == [("1", "1"), ("1", "2"), ("2", "1")]
    assert [r["outcome"] for r in voice] == ["ReadTimeout", "http_200", "http_200"]
    assert [r["api_result"] for r in voice] == ["unverified", "confirmed", "confirmed"]
    assert all(r["audio_bytes"] == "4" for r in voice)
    assert "notification" not in polling and "recipient" not in polling and "attempt" not in polling
    sleep.assert_called_once_with(2)
    assert post.call_count == 3
    assert all(call.kwargs["timeout"] == (10, 60) for call in post.call_args_list)
    assert "files" in post.call_args_list[1].kwargs
    assert post.call_args_list[2].kwargs["data"]["voice"] == "PRIVATE_FILE_ID"
    telemetry = " ".join(r.getMessage() for r in caplog.records if r.name == "radio.diagnostics")
    assert "PRIVATE" not in telemetry and "987654321" not in telemetry
    assert voice[0]["notification"] not in str(metrics.snapshot())


def test_identical_queued_phrases_get_distinct_ids_and_preserve_queue_trace(monkeypatch, caplog):
    service = make_service()
    service.pcm_to_ogg = Mock(return_value=b"OggS")
    post = Mock(return_value=response(voice=True))
    monkeypatch.setattr("requests.post", post)

    try:
        with caplog.at_level(logging.INFO, logger="radio.diagnostics"):
            first = service.notify("Ефір", "", "", "PRIVATE_PHRASE", b"\0\0", recipients=RECIPIENTS)
            second = service.notify("Ефір", "", "", "PRIVATE_PHRASE", b"\0\0", recipients=RECIPIENTS)
            service._outgoing_queue.join()
    finally:
        assert service.shutdown(timeout=2)

    assert first != second
    deliveries = records(caplog)
    assert [r["notification"] for r in deliveries] == [first, second]
    assert all(float(r["queue_wait_s"]) >= 0 for r in deliveries)
    assert all(r["attempt"] == "1" for r in deliveries)
    assert post.call_count == 2


def test_uncertain_voice_and_confirmed_text_share_notification_without_new_retries(monkeypatch, caplog):
    service = make_service()
    service.pcm_to_ogg = Mock(return_value=b"OggS")
    post = Mock(side_effect=[requests.ReadTimeout()] * 3 + [response()])
    monkeypatch.setattr("requests.post", post)
    sleep = Mock()
    monkeypatch.setattr("time.sleep", sleep)

    with caplog.at_level(logging.INFO, logger="radio.diagnostics"):
        service.send_voice(b"\0\0", "PRIVATE_PHRASE", None, RECIPIENTS)

    deliveries = records(caplog)
    assert len({r["notification"] for r in deliveries}) == 1
    assert [r["operation"] for r in deliveries] == ["sendVoice"] * 3 + ["sendMessage"]
    assert [r["attempt"] for r in deliveries[:3]] == ["1", "2", "3"]
    assert all(r["api_result"] == "unverified" for r in deliveries[:3])
    assert deliveries[-1]["api_result"] == "confirmed"
    assert "attempt" not in deliveries[-1]
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]
    assert post.call_count == 4
    fallback = post.call_args.kwargs["data"]
    assert fallback["text"] == "PRIVATE_PHRASE" and "parse_mode" not in fallback
