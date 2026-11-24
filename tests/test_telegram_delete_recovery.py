"""Збої видалення: лише тестова SQLite, підмінені HTTP та приватні фіктивні дані."""

import json
import logging
import sqlite3
import time
from unittest.mock import Mock

import pytest
import requests

from shared.diagnostics import RuntimeDiagnostics
from shared import telegram_common
from server import telegram_updates
from server.telegram import TelegramService
from shared.telegram_media_store import audio_file_id, is_hidden
from tests.test_miniapp_management import setup
from tests.test_telegram_media import phrase


def response(payload=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = {"ok": True} if payload is None else payload
    return result


def network_error(kind):
    if kind == "connection_reset":
        return requests.ConnectionError("SECRET_TOKEN PRIVATE_PHRASE", ConnectionResetError(54, "PRIVATE_PHRASE"))
    return getattr(requests, kind)("SECRET_TOKEN PRIVATE_PHRASE")


def callback(fix_id, message):
    return {"update_id": 41, "callback_query": {
        "id": "test-callback", "from": {"id": 1001}, "data": "del:" + fix_id,
        "message": message,
    }}


def state(path, fix_id):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT state FROM telegram_phrase_messages WHERE fix_id=?", (fix_id,)).fetchone()[0]


@pytest.fixture
def deletion(setup, monkeypatch):
    _, path = setup
    fix_id, message = phrase(path)
    metrics = RuntimeDiagnostics()
    monkeypatch.setattr(telegram_common, "runtime", metrics)
    monkeypatch.setattr(telegram_updates, "runtime", metrics)
    service = TelegramService("SECRET_TOKEN", 16000, path, logging.getLogger("test.telegram.deletion"))
    return service, metrics, path, fix_id, callback(fix_id, message)


def polling(monkeypatch, *batches):
    packets = [response({"result": batch}) for batch in batches]
    get = Mock(side_effect=[*packets, KeyboardInterrupt()])
    delays = []
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(time, "sleep", delays.append)
    return get, delays


@pytest.mark.parametrize("kind", ["connection_reset", "ReadTimeout", "ConnectTimeout", "Timeout"])
def test_network_delete_retries_keep_intent_and_log_without_traceback(deletion,monkeypatch,caplog,kind):
    service, metrics, path, fix_id, update = deletion
    get, delays = polling(monkeypatch,[update],[update])
    deletes = []
    answers = []

    def post(url, **kwargs):
        operation = url.rsplit("/",1)[-1]
        if operation == "deleteMessage":
            deletes.append(None)
            if len(deletes)==1:
                assert state(path,fix_id)=="deleting"
                raise network_error(kind)
            return response({"ok":False,"description":"Bad Request: message to delete not found"},400)
        assert operation == "answerCallbackQuery"
        answers.append(kwargs["data"]["text"])
        if len(answers)==1:
            assert state(path,fix_id)=="deleting"
        return response()

    monkeypatch.setattr(requests,"post",post)
    with caplog.at_level(logging.INFO):
        with pytest.raises(KeyboardInterrupt):
            service.callback_listener()
    assert answers==["Немає зв’язку — повторюю","Видалено"]
    assert state(path,fix_id)=="deleted"
    assert delays==[5]
    assert [c.kwargs["params"]["offset"] for c in get.call_args_list]==[None,None,42]
    counters=metrics.snapshot()["counters"]
    assert counters["telegram.deleteMessage.attempts"]==2
    assert counters["telegram.deleteMessage."+kind]==1
    assert counters["telegram.answerCallbackQuery.attempts"]==2
    assert counters["telegram.admin_delete.unconfirmed"]==1
    assert counters["telegram.admin_delete.confirmed"]==1
    assert not (path.parent/"telegram_failed_updates.jsonl").exists()
    warnings=[r for r in caplog.records if r.name==service.log.name and r.levelno>=logging.WARNING]
    assert len(warnings)==1 and warnings[0].exc_info is None
    assert "1/3" in warnings[0].getMessage() and "5" in warnings[0].getMessage()
    assert "SECRET_TOKEN" not in caplog.text and "PRIVATE_PHRASE" not in caplog.text
    assert "TG_DIAG operation=deleteMessage" in caplog.text


def test_callback_answer_failure_does_not_repeat_confirmed_deletion(deletion,monkeypatch):
    service, metrics, path, fix_id, update = deletion
    get,delays=polling(monkeypatch,[update])
    operations=[]
    def post(url,**kwargs):
        operation=url.rsplit("/",1)[-1];operations.append(operation)
        if operation=="deleteMessage":return response()
        raise requests.ReadTimeout("SECRET_TOKEN")
    monkeypatch.setattr(requests,"post",post)
    with pytest.raises(KeyboardInterrupt):service.callback_listener()
    assert state(path,fix_id)=="deleted"
    assert operations==["deleteMessage","answerCallbackQuery"]
    assert delays==[]
    assert [c.kwargs["params"]["offset"] for c in get.call_args_list]==[None,42]
    assert metrics.snapshot()["counters"]["telegram.answerCallbackQuery.ReadTimeout"]==1


def test_failed_error_answer_does_not_mask_original_delete_error(deletion,monkeypatch):
    service, metrics, path, fix_id, update = deletion
    def post(url,**kwargs):
        if url.endswith("/deleteMessage"):raise network_error("connection_reset")
        raise requests.ReadTimeout("SECRET_TOKEN")
    monkeypatch.setattr(requests,"post",post)
    with pytest.raises(requests.ConnectionError):service.handle_update(update)
    assert state(path,fix_id)=="deleting"
    assert metrics.snapshot()["counters"]["telegram.answerCallbackQuery.ReadTimeout"]==1


def test_exhausted_network_failures_keep_intent_and_quarantine_before_offset(deletion,monkeypatch,caplog):
    service,metrics,path,fix_id,update=deletion
    get,delays=polling(monkeypatch,[update],[update],[update])
    def post(url,**kwargs):
        if url.endswith("/deleteMessage"):raise network_error("connection_reset")
        return response()
    monkeypatch.setattr(requests,"post",post)
    with caplog.at_level(logging.INFO):
        with pytest.raises(KeyboardInterrupt):service.callback_listener()
    assert delays==[5,5]
    assert state(path,fix_id)=="deleting"
    assert [c.kwargs["params"]["offset"] for c in get.call_args_list]==[None,None,None,42]
    quarantine=path.parent/"telegram_failed_updates.jsonl"
    assert [json.loads(row) for row in quarantine.read_text().splitlines()]==[update]
    assert quarantine.stat().st_mode & 0o777 == 0o600
    assert metrics.snapshot()["counters"]["telegram.update_quarantined"]==1
    assert metrics.snapshot()["counters"]["telegram.admin_delete.unconfirmed"]==3
    assert all(record.exc_info is None for record in caplog.records)
    assert "SECRET_TOKEN" not in caplog.text and "PRIVATE_PHRASE" not in caplog.text


def test_explicit_delete_rejection_restores_visibility_without_new_retry_policy(deletion,monkeypatch):
    service,metrics,path,fix_id,update=deletion
    answers=[]
    def post(url,**kwargs):
        if url.endswith("/deleteMessage"):
            return response({"ok":False,"description":"Bad Request: message can't be deleted"},400)
        answers.append(kwargs["data"]["text"]);return response()
    monkeypatch.setattr(requests,"post",post)
    service.handle_update(update)
    assert state(path,fix_id)=="visible" and not is_hidden(path,fix_id)
    assert audio_file_id(path,fix_id)=="voice-test-id"
    assert answers==["Помилка"]
    assert metrics.snapshot()["counters"]["telegram.deleteMessage.http_400"]==1
    assert metrics.snapshot()["counters"]["telegram.admin_delete.rejected"]==1


def test_result_database_failure_keeps_intent_and_answers_error(deletion,monkeypatch):
    service,_,path,fix_id,update=deletion
    monkeypatch.setattr(requests,"post",Mock(return_value=response()))
    monkeypatch.setattr(telegram_updates,"deletion_result",Mock(side_effect=sqlite3.OperationalError("synthetic database failure")))
    with pytest.raises(sqlite3.Error):service.handle_update(update)
    assert state(path,fix_id)=="deleting"
    assert requests.post.call_args.kwargs["data"]["text"]=="Помилка"


def test_polling_network_warning_is_short_and_does_not_expose_exception(deletion,monkeypatch,caplog):
    service,metrics,_,_,_=deletion
    get=Mock(side_effect=[network_error("connection_reset"),response({"result":[]}),KeyboardInterrupt()])
    delays=[]
    monkeypatch.setattr(requests,"get",get)
    monkeypatch.setattr(time,"sleep",delays.append)
    with caplog.at_level(logging.INFO):
        with pytest.raises(KeyboardInterrupt):service.callback_listener()
    assert delays==[5]
    assert "SECRET_TOKEN" not in caplog.text and "PRIVATE_PHRASE" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert metrics.snapshot()["counters"]["telegram.getUpdates.connection_reset"]==1


def test_unexpected_programming_error_still_has_traceback(deletion,monkeypatch,caplog):
    service,_,_,_,update=deletion
    polling(monkeypatch,[update],[update])
    service.handle_update=Mock(side_effect=[RuntimeError("synthetic programming error"),None])
    with caplog.at_level(logging.INFO):
        with pytest.raises(KeyboardInterrupt):service.callback_listener()
    assert any(record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records)


def test_log_audit_counts_reset_from_diagnostic_without_needing_traceback(tmp_path):
    from diagnose_radio345 import audit_logs
    path=tmp_path/"radio.log"
    path.write_text("2026-11-15 12:20:00 [INFO] TG_DIAG operation=deleteMessage outcome=connection_reset elapsed_s=0.100\n"
                    "2026-11-15 12:20:00 [WARNING] Telegram недоступний (обрив з’єднання); повтор через 5 с, спроба 1/3\n")
    assert audit_logs([path])["counts"]["connection_reset"]==1


@pytest.mark.parametrize("payload",[None,[],{"unexpected":True}])
def test_invalid_delete_response_preserves_intent_and_answers_error(deletion,monkeypatch,payload):
    service,metrics,path,fix_id,update=deletion
    answers=[]
    def post(url,**kwargs):
        if url.endswith("/deleteMessage"):
            result=response()
            result.json.return_value=payload
            return result
        answers.append(kwargs["data"]["text"])
        return response()
    monkeypatch.setattr(requests,"post",post)
    with pytest.raises(ValueError):service.handle_update(update)
    assert state(path,fix_id)=="deleting"
    assert answers==["Помилка"]
    assert metrics.snapshot()["counters"]["telegram.admin_delete.unconfirmed"]==1


def test_polling_state_failure_keeps_traceback_and_existing_backoff(deletion,monkeypatch,caplog):
    service,_,_,_,_=deletion
    monkeypatch.setattr(requests,"get",Mock(side_effect=[sqlite3.OperationalError("synthetic state error"),response({"result":[]}),KeyboardInterrupt()]))
    delays=[]
    monkeypatch.setattr(time,"sleep",delays.append)
    with caplog.at_level(logging.INFO):
        with pytest.raises(KeyboardInterrupt):service.callback_listener()
    assert delays==[5]
    assert any(record.exc_info and record.exc_info[0] is sqlite3.OperationalError for record in caplog.records)
