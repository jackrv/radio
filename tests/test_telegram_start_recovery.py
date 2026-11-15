"""Відновлення /start після очищення чату: тимчасова SQLite та підмінений Telegram."""

import json
import logging
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from services.access_requests import register_request, request_token, resolve_request
from services.telegram import TelegramService
from services.telegram_maintenance import ensure_table
from services.user_notify_store import load_user_mode, save_user_mode
from tests.test_miniapp_management import setup

OLD_CARD = 42


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


def seed_card(path, chat_id):
    with sqlite3.connect(path) as db:
        ensure_table(db)
        db.execute("INSERT OR REPLACE INTO telegram_service_messages VALUES (?,'status',?,9999999999)",
                   (chat_id, OLD_CARD))


def start(user_id, *, update_id=1):
    return {"update_id": update_id, "message": {
        "message_id": 5 + update_id, "text": "/start", "from": {"id": user_id},
        "chat": {"id": user_id, "type": "private"},
    }}


def bot(path, *, edit_payload=None, new_failure=None, cleanup_failure=False, first_id=1000):
    service = TelegramService("test", 16000, path, logging.getLogger("test"))
    sent = []

    def request(method, operation, **kwargs):
        data = kwargs["data"]
        if operation == "editMessageText":
            payload = edit_payload or {"ok": True, "result": {"message_id": OLD_CARD}}
            return response(payload, 200 if payload["ok"] else 400)
        if operation == "sendMessage":
            if new_failure is not None:
                if isinstance(new_failure, BaseException):
                    raise new_failure
                return response(new_failure, 200)
            mid = first_id + len(sent)
            sent.append({**data, "message_id": mid})
            return response({"ok": True, "result": {"message_id": mid, "chat": {"id": data["chat_id"]}}})
        if operation == "deleteMessage":
            if cleanup_failure and data["message_id"] == OLD_CARD:
                raise requests.ReadTimeout()
            return response({"ok": True})
        raise AssertionError(operation)

    service._request = Mock(side_effect=request)
    return service, sent


@pytest.mark.parametrize("user_id,role", [(1001,"admin"), (2002,"viewer"), (3003,"editor")])
@pytest.mark.parametrize("edit_payload", [
    {"ok": True, "result": {"message_id": OLD_CARD}},
    {"ok": False, "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup"},
])
def test_start_creates_visible_new_card_even_if_editing_old_card_would_succeed(setup,user_id,role,edit_payload):
    _, path = setup
    seed_card(path,user_id)
    save_user_mode(path,user_id,"off")
    service, sent = bot(path, edit_payload=edit_payload)
    service.handle_update(start(user_id))
    assert len(sent) == 1
    assert sent[0]["chat_id"] == user_id
    assert json.loads(sent[0]["reply_markup"])["inline_keyboard"][0][0]["web_app"]
    operations = [call.args[1] for call in service._request.call_args_list]
    assert operations[0] == "sendMessage"
    assert "editMessageText" not in operations
    assert load_user_mode(path,user_id) == (role,"off")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT message_id FROM telegram_service_messages WHERE chat_id=? AND slot='status'",
                          (user_id,)).fetchone()[0] == sent[0]["message_id"]
    deleted = [call.kwargs["data"]["message_id"] for call in service._request.call_args_list if call.args[1]=="deleteMessage"]
    assert OLD_CARD in deleted and start(user_id)["message"]["message_id"] in deleted
    assert sent[0]["message_id"] not in deleted


@pytest.mark.parametrize("state",["pending","rejected"])
def test_start_replies_fresh_without_repeating_access_request_or_granting_access(setup,state):
    _,path=setup
    register_request(path,{"id":4004})
    if state=="rejected":
        resolve_request(path,1001,4004,None)
    seed_card(path,4004)
    previous=request_token(path,4004)
    service,sent=bot(path)
    service.handle_update(start(4004))
    assert len(sent)==1 and sent[0]["chat_id"]==4004
    assert request_token(path,4004)==previous
    assert load_user_mode(path,4004) is None
    assert "reply_markup" not in sent[0]


def test_repeated_start_does_not_send_duplicate_cards_to_admin(setup):
    _,path=setup
    service,sent=bot(path)
    service.handle_update(start(4004,update_id=1))
    service.handle_update(start(4004,update_id=2))
    assert len([m for m in sent if m["chat_id"]==1001])==1
    assert len([m for m in sent if m["chat_id"]==4004])==2


def test_start_recovery_survives_bot_restart(setup):
    _,path=setup
    service,sent=bot(path)
    service.handle_update(start(1001))
    recovered,new_sent=bot(path,first_id=2000)
    recovered.handle_update(start(1001,update_id=2))
    assert len(new_sent)==1 and new_sent[0]["message_id"]!=sent[0]["message_id"]
    assert recovered._request.call_args_list[0].args[1]=="sendMessage"


@pytest.mark.parametrize("failure",[
    requests.ReadTimeout(), {"ok":False,"description":"synthetic rejection"},
    {"ok":True,"result":True}, ["invalid response"],
])
def test_failed_start_reply_preserves_command_and_old_reference(setup,failure):
    _,path=setup
    seed_card(path,1001)
    service,_=bot(path,new_failure=failure,edit_payload={"ok":False,"description":"Bad Request: message to edit not found"})
    service.handle_update(start(1001))
    assert all(call.args[1]!="deleteMessage" for call in service._request.call_args_list)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT message_id FROM telegram_service_messages WHERE chat_id=1001 AND slot='status'").fetchone()[0]==OLD_CARD


def test_old_card_cleanup_failure_does_not_cancel_new_start_reply(setup):
    _,path=setup
    seed_card(path,1001)
    service,sent=bot(path,cleanup_failure=True)
    service.handle_update(start(1001))
    assert len(sent)==1
    deleted=[call.kwargs["data"]["message_id"] for call in service._request.call_args_list if call.args[1]=="deleteMessage"]
    assert start(1001)["message"]["message_id"] in deleted
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT message_id FROM telegram_service_messages WHERE chat_id=1001 AND slot='status'").fetchone()[0]==sent[0]["message_id"]


def test_start_does_not_restore_revoked_role_when_recovering_chat(setup):
    _,path=setup
    with sqlite3.connect(path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=3003")
    seed_card(path,3003)
    service,sent=bot(path)
    service.handle_update(start(3003))
    own=[message for message in sent if message["chat_id"]==3003]
    assert len(own)==1 and "reply_markup" not in own[0]
    assert load_user_mode(path,3003) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT enabled FROM telegram_users WHERE user_id=3003").fetchone()[0]==0
