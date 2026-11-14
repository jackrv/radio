import json
import logging
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
import requests

from core.diagnostics import RuntimeDiagnostics, error_kind
from diagnose_radio345 import audit_database, audit_logs
from database import init_db as init_dictionaries
from services.event_store import init_db
from services.sqlite_connection import get_db
from services.user_store import init_users_db, bootstrap_admin
from services.telegram import TelegramService
from tests.test_telegram_outbox import make_service


def test_nested_reset_is_classified_without_serializing_exception():
    error = requests.ConnectionError("SECRET_TOKEN", ConnectionResetError(54, "PRIVATE_TEXT"))
    assert error_kind(error) == "connection_reset"
    assert error_kind(requests.ReadTimeout("SECRET_TOKEN")) == "ReadTimeout"


def test_snapshot_is_bounded_and_thread_safe(tmp_path):
    metrics = RuntimeDiagnostics()
    def write(_):
        for _ in range(200):
            metrics.count("test.count")
            metrics.observe("test.latency", 0.25)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(write, range(4)))
    metrics.configure(tmp_path / "diagnostics.json")
    data = json.loads((tmp_path / "diagnostics.json").read_text())
    assert data["counters"]["test.count"] == 800
    assert data["timings"]["test.latency"]["count"] == 800
    assert data["timings"]["test.latency"]["recent_p95_s"] == .25
    assert len(metrics.timings["test.latency"]["recent_s"]) == 128
    assert (tmp_path / "diagnostics.json").stat().st_mode & 0o777 == 0o600


def test_snapshot_io_failure_does_not_break_application(tmp_path):
    metrics = RuntimeDiagnostics()
    metrics.configure(tmp_path / "missing" / "diagnostics.json")
    metrics.count("still_running")
    metrics.flush(force=True)
    assert metrics.snapshot()["counters"]["still_running"] == 1


def test_network_measurements_leave_retries_unchanged_and_redact_data(monkeypatch, caplog):
    import services.telegram as telegram
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(telegram, "runtime", metrics)
    s = make_service()
    s.pcm_to_ogg = Mock(return_value=b"ogg")
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True}
    error = requests.ConnectionError("SECRET", ConnectionResetError(54, "PRIVATE"))
    with caplog.at_level(logging.INFO, logger="radio.diagnostics"):
        with patch("services.telegram.requests.post", side_effect=[error, response]) as post:
            with patch("services.telegram.time.sleep") as sleep:
                s.send_voice(b"\0\0", "PRIVATE_PHRASE", "db:1")
    assert post.call_count == 2
    sleep.assert_called_once_with(2)
    counters = metrics.snapshot()["counters"]
    assert counters["telegram.sendVoice.attempts"] == 2
    assert counters["telegram.sendVoice.connection_reset"] == 1
    assert counters["telegram.delivery_uncertain"] == 1
    assert counters["telegram.api_confirmed"] == 1
    telemetry = " ".join(r.getMessage() for r in caplog.records if r.name == "radio.diagnostics")
    assert "SECRET" not in telemetry and "PRIVATE" not in telemetry
    assert "elapsed_s=" in telemetry


def test_ogg_failure_records_fallback_without_changing_plain_text(monkeypatch):
    import services.telegram as telegram
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(telegram, "runtime", metrics)
    s = make_service()
    s.pcm_to_ogg = Mock(side_effect=FileNotFoundError)
    s.send_text = Mock()
    s.send_voice(b"\0\0", "private")
    assert metrics.snapshot()["counters"]["telegram.text_fallback"] == 1
    assert s.send_text.call_args.kwargs["parse_mode"] is None


def test_http_200_api_rejection_is_not_counted_as_confirmed(monkeypatch):
    import services.telegram as telegram
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(telegram, "runtime", metrics)
    s = make_service()
    s.pcm_to_ogg = Mock(return_value=b"ogg")
    response = Mock(status_code=200)
    response.json.return_value = {"ok": False}
    with patch("services.telegram.requests.post", return_value=response) as post:
        s.send_voice(b"\0\0", "private")
    counters = metrics.snapshot()["counters"]
    assert counters["telegram.sendVoice.api_rejected"] == 1
    assert counters.get("telegram.api_confirmed", 0) == 0
    assert post.call_count == 1  # Observability does not introduce new retries.


def test_queue_overflow_and_wait_are_measured(monkeypatch):
    import services.telegram as telegram
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(telegram, "runtime", metrics)
    s = make_service()
    # Hold worker creation so the queue can be filled deterministically.
    s._outgoing_thread = Mock()
    s._outgoing_thread.is_alive.return_value = True
    for _ in range(33):
        s.notify("", "", "", "private", b"\0\0")
    assert metrics.snapshot()["counters"]["telegram.queue_overflow"] == 1
    s.send_voice = Mock()
    s._outgoing_stop.set()
    s._outgoing_loop()
    assert s.send_voice.call_count == 32
    assert metrics.snapshot()["timings"]["telegram.queue_wait"]["count"] == 32
    assert metrics.snapshot()["gauges"]["telegram.queue_depth"] == 0


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "events.db"
    init_db(path)
    init_dictionaries(path)
    init_users_db(path)
    bootstrap_admin(path, 1001)
    return path


def test_readonly_audit_and_temporary_restore_preserve_database(database):
    before = database.read_bytes()
    report = audit_database(database, verify_backup=True)
    assert report["checks"]["integrity_ok"] is True
    assert report["checks"]["backup_integrity_ok"] is True
    assert report["checks"]["backup_counts_match"] is True
    assert report["checks"]["confirmation_mismatches"] == 0
    assert database.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    import sqlite3
    with pytest.raises(sqlite3.Error):
        audit_database(path)
    assert not path.exists()


def test_audit_finds_vote_inconsistency_without_exposing_words(database):
    with get_db(database) as db:
        db.execute("INSERT INTO word_corrections (original,corrected,confirmations,status) VALUES ('PRIVATE','SECRET',9,'active')")
    report = audit_database(database)
    assert report["checks"]["confirmation_mismatches"] == 1
    assert "PRIVATE" not in json.dumps(report) and "SECRET" not in json.dumps(report)


def test_log_audit_reports_counts_only(tmp_path):
    path = tmp_path / "radio.log"
    path.write_text("2026-11-14 13:00:00 [WARNING] LLM отклонена: PRIVATE_PHRASE\n"
                    "2026-11-14 13:01:00 [WARNING] Сбой связи в callback_listener SECRET ConnectionResetError(54)\n")
    report = audit_logs([path])
    assert report["counts"]["llm_rejected"] == 1
    assert report["counts"]["connection_reset"] == 1
    assert "PRIVATE" not in json.dumps(report) and "SECRET" not in json.dumps(report)
