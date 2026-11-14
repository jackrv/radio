"""Особисті режими сповіщень і розсилка всім користувачам — без мережі, тимчасові бази."""

import json
import logging
import sqlite3
import threading
from unittest.mock import Mock

import pytest
import requests

from core.event_processor import EventProcessor
from services.notify_mode_store import save_notify_mode
from services.sqlite_connection import get_db
from services.telegram import TelegramService
from services.user_notify_store import (
    DEFAULT_USER_MODE, allowed_modes, apply_telegram_user_mode, list_recipients,
    load_user_mode, save_user_mode,
)
from services.user_store import bootstrap_admin, init_users_db

ADMIN, EDITOR, VIEWER = 1001, 2002, 3003


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "events.db"
    init_users_db(path)
    bootstrap_admin(path, ADMIN)
    with get_db(path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (?, 'editor'), (?, 'viewer')", (EDITOR, VIEWER))
    return path


def modes(path):
    return {r["chat_id"]: r["mode"] for r in list_recipients(path)}


# ---------- Сховище ----------

def test_defaults_keep_admin_legacy_mode_and_give_others_commands(db_path):
    save_notify_mode(db_path, "watch")
    assert modes(db_path) == {ADMIN: "watch", EDITOR: DEFAULT_USER_MODE, VIEWER: DEFAULT_USER_MODE}


def test_all_only_for_admin(db_path):
    assert "all" in allowed_modes("admin") and "all" not in allowed_modes("editor")
    save_user_mode(db_path, ADMIN, "all")
    with pytest.raises(PermissionError):
        save_user_mode(db_path, EDITOR, "all")
    with pytest.raises(PermissionError):
        save_user_mode(db_path, VIEWER, "all")
    assert load_user_mode(db_path, EDITOR) == ("editor", DEFAULT_USER_MODE)


def test_demoted_admin_loses_all(db_path):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (4004, 'admin')")
    save_user_mode(db_path, 4004, "all")
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET role='viewer' WHERE user_id=4004")
    assert load_user_mode(db_path, 4004) == ("viewer", DEFAULT_USER_MODE)


def test_disabled_and_unknown_users_get_nothing(db_path):
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=?", (VIEWER,))
    assert VIEWER not in modes(db_path)
    assert load_user_mode(db_path, VIEWER) is None
    with pytest.raises(LookupError):
        save_user_mode(db_path, 9999, "watch")


def test_telegram_mode_command_is_idempotent(db_path):
    assert apply_telegram_user_mode(db_path, EDITOR, 50, "watch") is True
    assert apply_telegram_user_mode(db_path, EDITOR, 50, "off") is False
    assert load_user_mode(db_path, EDITOR)[1] == "watch"


# ---------- Отримувачі ----------

def make_service(db_path, chat_id=str(ADMIN)):
    service = TelegramService(
        bot_token="token", chat_id=chat_id, sample_rate=16000, db_path=db_path,
        log=logging.getLogger("test"), state_lock=threading.Lock(), get_notify_mode=lambda: "all", set_notify_mode=lambda mode: None,
    )
    service.pcm_to_ogg = lambda pcm: b"ogg"
    return service


def wants_commands(mode):
    return mode in ("all", "commands")


def test_recipients_follow_personal_modes_work_chat_first(db_path):
    save_user_mode(db_path, VIEWER, "watch")
    service = make_service(db_path)
    picked = service.recipients_for(wants_commands)
    assert [r["chat_id"] for r in picked] == [ADMIN, EDITOR]
    save_user_mode(db_path, ADMIN, "off")
    assert [r["chat_id"] for r in service.recipients_for(lambda m: True)] == [EDITOR, VIEWER]


def test_without_users_work_chat_uses_global_mode(tmp_path):
    service = make_service(tmp_path / "empty.db", chat_id="555")
    [recipient] = service.recipients_for(lambda m: m == "all")
    assert recipient["chat_id"] == "555" and recipient.get("legacy")


def test_after_registration_only_database_decides_recipients(db_path):
    service = make_service(db_path, chat_id="777")
    chats = [str(r["chat_id"]) for r in service.recipients_for(wants_commands)]
    assert "777" not in chats and str(EDITOR) in chats


def test_disabled_env_owner_gets_nothing_and_cannot_change_modes(db_path):
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=?", (ADMIN,))
    modes_seen = {"v": "all"}
    service = make_service(db_path)
    service.set_notify_mode = lambda mode: modes_seen.update(v=mode)
    service.send_text = Mock()
    assert str(ADMIN) not in [str(r["chat_id"]) for r in service.recipients_for(lambda m: True)]
    service.handle_update(command(ADMIN, "/off", 300))
    assert modes_seen["v"] == "all"
    service.send_text.assert_not_called()


# ---------- Надсилання ----------

def ok_response(file_id="FILE1", message_id=10, chat_id=ADMIN):
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"ok": True, "result": {
        "message_id": message_id, "chat": {"id": chat_id}, "voice": {"file_id": file_id},
    }}
    return response


def forbidden():
    response = Mock(status_code=403)
    response.raise_for_status.side_effect = requests.HTTPError("403", response=response)
    return response


def buttons(markup):
    return [b.get("callback_data") or "web_app" for row in json.loads(markup)["inline_keyboard"] for b in row]


def test_voice_uploaded_once_then_reused_with_role_keyboards(db_path):
    service = make_service(db_path)
    service._request = Mock(side_effect=[ok_response(), ok_response(chat_id=EDITOR), ok_response(chat_id=VIEWER)])
    recipients = service.recipients_for(lambda m: True)

    service.send_voice(b"\0\0", "Липка 700", "db:7", recipients)

    calls = service._request.call_args_list
    assert [c.kwargs["data"]["chat_id"] for c in calls] == [ADMIN, EDITOR, VIEWER]
    assert "files" in calls[0].kwargs and "files" not in calls[1].kwargs
    assert calls[1].kwargs["data"]["voice"] == "FILE1"
    assert buttons(calls[0].kwargs["data"]["reply_markup"]) == ["del:db:7", "web_app"]
    assert buttons(calls[1].kwargs["data"]["reply_markup"]) == ["del:db:7", "web_app"]
    assert buttons(calls[2].kwargs["data"]["reply_markup"]) == ["del:db:7"]
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT chat_id, message_id, voice_file_id FROM telegram_phrase_messages WHERE fix_id='db:7'").fetchone()
    assert row == (str(ADMIN), 10, "FILE1"), "прив’язка лише до робочого чату"


def test_blocked_user_is_skipped_without_retries_or_text(db_path, monkeypatch):
    monkeypatch.setattr("services.telegram.time.sleep", Mock())
    service = make_service(db_path)
    service.send_text = Mock()
    service._request = Mock(side_effect=[ok_response(), forbidden(), ok_response(chat_id=VIEWER)])
    service.send_voice(b"\0\0", "фраза", "db:1", service.recipients_for(lambda m: True))
    assert service._request.call_count == 3
    service.send_text.assert_not_called()


def test_voice_for_other_user_keeps_audio_for_editor(db_path):
    save_user_mode(db_path, ADMIN, "off")
    service = make_service(db_path)
    service._request = Mock(return_value=ok_response(file_id="FILE9", chat_id=EDITOR))
    service.send_voice(b"\0\0", "фраза", "db:9", service.recipients_for(lambda m: True))
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT chat_id, voice_file_id FROM telegram_phrase_messages WHERE fix_id='db:9'").fetchone() == (None, "FILE9")


def test_recipient_switched_off_before_sending_is_skipped(db_path):
    service = make_service(db_path)
    recipients = service.recipients_for(lambda m: True)
    save_user_mode(db_path, VIEWER, "off")
    service._request = Mock(return_value=ok_response())
    service.send_voice(b"\0\0", "фраза", "db:2", recipients)
    assert VIEWER not in [c.kwargs["data"]["chat_id"] for c in service._request.call_args_list]


def test_event_processor_passes_personal_recipients():
    class Dictionary:
        def __init__(self, found):
            self.found = found

        def find(self, text):
            return self.found

    telegram = Mock()
    telegram.register_phrase_for_fix.return_value = "db:42"
    seen = []

    def select(wants):
        seen.append({mode: wants(mode) for mode in ("all", "watch", "commands", "off")})
        return [{"chat_id": EDITOR, "role": "editor", "mode": "commands"}]

    processor = EventProcessor(
        callsigns=Dictionary([("липка", "700", 100)]), commands=Dictionary([]), watch=Dictionary([]),
        short_garbage=set(), save_event=Mock(return_value=42), telegram_service=telegram,
        fresh=Mock(return_value=True), state_lock=threading.RLock(), get_notify_mode=lambda: "off",
        db_path="x.db", log_callsigns_only=True, log=Mock(), select_recipients=select,
    )
    processor.process("Липка 700", b"pcm")
    assert seen == [{"all": True, "watch": False, "commands": True, "off": False}]
    assert telegram.notify.call_args.kwargs["recipients"][0]["chat_id"] == EDITOR

    telegram.reset_mock()
    processor.select_recipients = lambda wants: []
    processor.process("Липка 700", b"pcm")
    telegram.notify.assert_not_called()
    telegram.register_phrase_for_fix.assert_not_called()


# ---------- Команди в Telegram ----------

def command(user_id, text, update_id=100):
    return {"update_id": update_id, "message": {
        "text": text, "from": {"id": user_id}, "chat": {"id": user_id, "type": "private"},
    }}


def test_editor_cannot_enable_all_but_can_choose_watch(db_path):
    service = make_service(db_path)
    service.send_text = Mock()
    service.handle_update(command(EDITOR, "/all", 100))
    assert "лише адміністратору" in service.send_text.call_args.args[0]
    assert service.send_text.call_args.kwargs["chat_id"] == EDITOR
    assert load_user_mode(db_path, EDITOR)[1] == DEFAULT_USER_MODE

    service.handle_update(command(EDITOR, "спостереження", 101))
    assert load_user_mode(db_path, EDITOR)[1] == "watch"
    assert "WATCH" in service.send_text.call_args.args[0]


def test_admin_command_changes_only_own_mode(db_path):
    service = make_service(db_path)
    service.send_text = Mock()
    service.handle_update(command(ADMIN, "/off", 100))
    assert load_user_mode(db_path, ADMIN)[1] == "off"
    assert load_user_mode(db_path, VIEWER)[1] == DEFAULT_USER_MODE
    assert service.send_text.call_args.kwargs["chat_id"] is None, "відповідь у робочий чат"


def test_stranger_mode_command_is_ignored(db_path):
    service = make_service(db_path)
    service.send_text = Mock()
    service.handle_update(command(9999, "/watch"))
    service.send_text.assert_not_called()


def test_user_delete_button_removes_only_own_copy(db_path):
    service = make_service(db_path)
    service._request = Mock(return_value=Mock(status_code=200))
    service.handle_update({"update_id": 5, "callback_query": {
        "id": "cb", "data": "del:db:3", "from": {"id": VIEWER},
        "message": {"message_id": 44, "chat": {"id": VIEWER, "type": "private"}},
    }})
    deleted = [c for c in service._request.call_args_list if c.args[1] == "deleteMessage"]
    assert deleted[0].kwargs["data"] == {"chat_id": str(VIEWER), "message_id": 44}
    with sqlite3.connect(db_path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        if "telegram_phrase_messages" in tables:
            assert db.execute("SELECT COUNT(*) FROM telegram_phrase_messages").fetchone()[0] == 0


@pytest.mark.parametrize("data", ["fix_db:3"])
def test_other_buttons_in_user_chat_are_ignored(db_path, data):
    service = make_service(db_path)
    service._request = Mock()
    service.handle_update({"update_id": 6, "callback_query": {
        "id": "cb", "data": data, "from": {"id": EDITOR},
        "message": {"message_id": 1, "chat": {"id": EDITOR, "type": "private"}},
    }})
    service._request.assert_not_called()
