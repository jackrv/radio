"""Особисті режими сповіщень і розсилка всім користувачам — без мережі, тимчасові бази."""

import json
import logging
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from core.event_processor import EventProcessor
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

def test_defaults_admin_all_others_commands(db_path):
    assert modes(db_path) == {ADMIN: "all", EDITOR: DEFAULT_USER_MODE, VIEWER: DEFAULT_USER_MODE}


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

def make_service(db_path):
    service = TelegramService(
        bot_token="token",
        sample_rate=16000,
        db_path=db_path,
        log=logging.getLogger("test"),
    )
    service.pcm_to_ogg = lambda pcm: b"ogg"
    return service


def wants_commands(mode):
    return mode in ("all", "commands")


def test_recipients_follow_personal_modes(db_path):
    save_user_mode(db_path, VIEWER, "watch")
    service = make_service(db_path)
    picked = service.recipients_for(wants_commands)
    assert [r["chat_id"] for r in picked] == [ADMIN, EDITOR]
    save_user_mode(db_path, ADMIN, "off")
    assert [r["chat_id"] for r in service.recipients_for(lambda m: True)] == [EDITOR, VIEWER]


def test_without_users_nobody_receives(tmp_path):
    assert make_service(tmp_path / "empty.db").recipients_for(lambda m: True) == []


def test_disabled_env_owner_gets_nothing_and_cannot_change_modes(db_path):
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=?", (ADMIN,))
    service = make_service(db_path)
    service.send_text = Mock()
    assert str(ADMIN) not in [str(r["chat_id"]) for r in service.recipients_for(lambda m: True)]
    service.handle_update(command(ADMIN, "/off", 300))
    service.send_text.assert_not_called()
    with get_db(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM telegram_user_settings WHERE user_id=?", (ADMIN,)).fetchone()[0] == 0


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
    assert row == (None, None, "FILE1"), "редактор зберігає лише file_id, без прив’язки до чату"


def test_blocked_user_is_skipped_without_retries_or_text(db_path, monkeypatch):
    monkeypatch.setattr("time.sleep", Mock())
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
        fresh=Mock(return_value=True),
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
    assert service.send_text.call_args.kwargs["chat_id"] == ADMIN


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


@pytest.mark.parametrize("data", ["del:db:3", "acc:viewer:2002:01234567"])
@pytest.mark.parametrize("chat", [
    {"id": ADMIN},
    {"id": ADMIN, "type": "group"},
    {"id": EDITOR, "type": "private"},
])
def test_callbacks_require_the_actors_private_chat(db_path, data, chat):
    service = make_service(db_path)
    service._request = Mock()
    service._handle_access_decision = Mock()
    service.handle_update({"callback_query": {
        "id": "cb", "data": data, "from": {"id": ADMIN},
        "message": {"message_id": 44, "chat": chat},
    }})
    service._request.assert_not_called()
    service._handle_access_decision.assert_not_called()


@pytest.mark.parametrize("data", ["fix_db:3"])
def test_other_buttons_in_user_chat_are_ignored(db_path, data):
    service = make_service(db_path)
    service._request = Mock()
    service.handle_update({"update_id": 6, "callback_query": {
        "id": "cb", "data": data, "from": {"id": EDITOR},
        "message": {"message_id": 1, "chat": {"id": EDITOR, "type": "private"}},
    }})
    service._request.assert_not_called()


# ---------- Рецензія 2: відмова при помилці БД і перевірка черги ----------

def test_database_error_means_nobody_receives(db_path):
    from unittest.mock import patch

    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=?", (ADMIN,))
    service = make_service(db_path)
    with patch("services.telegram_outbox.list_recipients", side_effect=sqlite3.OperationalError("locked")):
        assert service.recipients_for(lambda m: True) == []


def test_queued_phrase_rechecks_role_and_mode_before_sending(db_path):
    save_user_mode(db_path, ADMIN, "all")
    service = make_service(db_path)
    queued = [r for r in service.recipients_for(lambda m: m == "all") if r["chat_id"] == ADMIN]
    assert queued, "фраза підходить лише під режим all"
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET role='viewer' WHERE user_id=?", (ADMIN,))
    service._request = Mock(return_value=ok_response())
    service.send_voice(b"\0\0", "фраза", "db:4", queued)
    service._request.assert_not_called()


def test_keyboard_follows_current_role_at_send_time(db_path):
    service = make_service(db_path)
    queued = [r for r in service.recipients_for(lambda m: True) if r["chat_id"] == EDITOR]
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET role='viewer' WHERE user_id=?", (EDITOR,))
    service._request = Mock(return_value=ok_response(chat_id=EDITOR))
    service.send_voice(b"\0\0", "фраза", "db:6", queued)
    assert buttons(service._request.call_args.kwargs["data"]["reply_markup"]) == ["del:db:6"]


def test_hung_ffmpeg_falls_back_to_text(db_path):
    import subprocess

    service = make_service(db_path)
    service.pcm_to_ogg = Mock(side_effect=subprocess.TimeoutExpired("ffmpeg", 30))
    service.send_text = Mock(return_value=True)
    service.send_voice(b"\0\0", "фраза", "db:8", service.recipients_for(lambda m: True))
    assert service.send_text.call_count == 3


@pytest.mark.parametrize("change", ["enabled=0", "role='viewer'"])
def test_admin_alert_retries_recheck_current_access(db_path, monkeypatch, change):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (4004, 'admin')")
    service = make_service(db_path)
    service._request = Mock(side_effect=[requests.Timeout(), Mock(status_code=200)])

    def revoke_before_retry(_):
        with get_db(db_path) as db:
            db.execute(f"UPDATE telegram_users SET {change} WHERE user_id=?", (ADMIN,))

    monkeypatch.setattr("services.telegram_common.time.sleep", revoke_before_retry)
    thread = service.notify_admin_async("Службове повідомлення", delays=(0, 1))
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [c.kwargs["data"]["chat_id"] for c in service._request.call_args_list] == [ADMIN, 4004]


def test_admin_alert_recovers_after_unavailable_database(db_path, monkeypatch):
    service = make_service(db_path)
    service._admin_chats = Mock(side_effect=[[], [ADMIN]])
    service._request = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr("services.telegram_common.time.sleep", lambda _: None)
    thread = service.notify_admin_async("Службове повідомлення", delays=(0, 1))
    thread.join(timeout=5)
    assert not thread.is_alive()
    service._request.assert_called_once()
    assert service._request.call_args.kwargs["data"]["chat_id"] == ADMIN



def test_older_mode_command_cannot_overwrite_newer(db_path):
    service = make_service(db_path)
    service.send_text = Mock()
    service.handle_update(command(EDITOR, "/watch", 100))
    service.handle_update(command(EDITOR, "/off", 102))
    service.handle_update(command(EDITOR, "/commands", 101))
    assert load_user_mode(db_path, EDITOR)[1] == "off"
    assert service.send_text.call_count == 2


def test_failed_mode_transaction_changes_nothing(db_path):
    from services.telegram_offset_store import load_telegram_offset

    assert load_telegram_offset(db_path) is None
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TRIGGER fail_offset BEFORE INSERT ON telegram_poll_state
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    service = make_service(db_path)
    service.send_text = Mock()
    with pytest.raises(sqlite3.IntegrityError):
        service.handle_update(command(EDITOR, "/watch", 100))
    assert load_user_mode(db_path, EDITOR)[1] == DEFAULT_USER_MODE
    service.send_text.assert_not_called()
