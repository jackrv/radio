"""/start від нових користувачів і рішення адміністратора — лише тимчасові бази."""

import json
import logging
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from services.access_requests import (
    MAX_PENDING_REQUESTS, REQUEST_RETRY_SECONDS, register_request, resolve_request,
)
from services.sqlite_connection import get_db
from services.telegram import TelegramService
from services.user_store import bootstrap_admin, init_users_db

ADMIN = 1001
ADMIN_CHAT = "1001"
NEWBIE = {"id": 2002, "first_name": "Іван", "last_name": "Петренко", "username": "ivan_p"}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "events.db"
    init_users_db(path)
    bootstrap_admin(path, ADMIN)
    return path


def users(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT user_id, role, enabled FROM telegram_users ORDER BY user_id").fetchall()


# ---------- Сховище ----------

def test_new_request_then_pending_without_second_notification(db_path):
    assert register_request(db_path, NEWBIE, now=100) == ("new", None)
    assert register_request(db_path, NEWBIE, now=200) == ("pending", None)


def test_stale_pending_request_is_sent_again(db_path):
    register_request(db_path, NEWBIE, now=100)
    assert register_request(db_path, NEWBIE, now=100 + REQUEST_RETRY_SECONDS + 1) == ("new", None)


def test_member_is_recognised_and_request_cleared(db_path):
    assert register_request(db_path, {"id": ADMIN}, now=1) == ("member", "admin")


def test_grant_adds_user_with_profile(db_path):
    register_request(db_path, NEWBIE, now=1)
    result = resolve_request(db_path, ADMIN, 2002, "editor")
    assert result["status"] == "granted"
    assert (2002, "editor", 1) in users(db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT telegram_name, username FROM telegram_user_profiles WHERE user_id=2002").fetchone() == ("Іван Петренко", "ivan_p")
        assert db.execute("SELECT COUNT(*) FROM telegram_access_requests").fetchone()[0] == 0
    assert register_request(db_path, NEWBIE) == ("member", "editor")


def test_repeated_grant_is_harmless(db_path):
    register_request(db_path, NEWBIE, now=1)
    resolve_request(db_path, ADMIN, 2002, "viewer")
    assert resolve_request(db_path, ADMIN, 2002, "viewer")["status"] == "already"
    assert users(db_path).count((2002, "viewer", 1)) == 1


def test_disabled_user_is_reenabled_on_grant(db_path):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role, enabled) VALUES (2002, 'viewer', 0)")
    assert register_request(db_path, NEWBIE, now=1) == ("new", None)
    assert resolve_request(db_path, ADMIN, 2002, "editor")["status"] == "granted"
    assert (2002, "editor", 1) in users(db_path)


def test_reject_blocks_requests_for_a_day(db_path):
    register_request(db_path, NEWBIE, now=1)
    assert resolve_request(db_path, ADMIN, 2002, None, now=10)["status"] == "rejected"
    assert register_request(db_path, NEWBIE, now=20) == ("rejected", None)
    assert register_request(db_path, NEWBIE, now=10 + REQUEST_RETRY_SECONDS + 1) == ("new", None)
    assert (2002, "viewer", 1) not in users(db_path)


def test_admin_cannot_be_changed_through_request(db_path):
    assert resolve_request(db_path, ADMIN, ADMIN, "viewer")["status"] == "admin"
    assert (ADMIN, "admin", 1) in users(db_path)


def test_only_admin_can_decide(db_path):
    register_request(db_path, NEWBIE, now=1)
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (3003, 'editor')")
    with pytest.raises(PermissionError):
        resolve_request(db_path, 3003, 2002, "editor")
    with pytest.raises(PermissionError):
        resolve_request(db_path, 2002, 2002, "editor")
    assert all(row[0] != 2002 for row in users(db_path))


def test_admin_role_cannot_be_granted(db_path):
    with pytest.raises(ValueError):
        resolve_request(db_path, ADMIN, 2002, "admin")


def test_pending_requests_are_limited(db_path):
    for user_id in range(5000, 5000 + MAX_PENDING_REQUESTS):
        assert register_request(db_path, {"id": user_id}, now=1)[0] == "new"
    assert register_request(db_path, {"id": 9999}, now=2) == ("limit", None)


# ---------- Telegram ----------

def make_service(db_path):
    service = TelegramService(
        bot_token="token", chat_id=ADMIN_CHAT, sample_rate=16000, db_path=db_path,
        log=logging.getLogger("test"), state_lock=threading.Lock(), get_notify_mode=lambda: "all", set_notify_mode=lambda mode: None,
    )
    service.send_text = Mock()
    service._request = Mock()
    return service


def start_from(sender, text="/start"):
    return {"update_id": 1, "message": {
        "message_id": 5, "text": text, "from": sender,
        "chat": {"id": sender["id"], "type": "private"},
    }}


def sent(service):
    return [(call.args[0], call.kwargs.get("chat_id"), call.kwargs.get("reply_markup")) for call in service.send_text.call_args_list]


def test_start_from_stranger_notifies_admin_once(db_path):
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    service.handle_update(start_from(NEWBIE, "/start@radio345_bot"))

    messages = sent(service)
    to_admin = [m for m in messages if m[1] is None]
    to_user = [m for m in messages if m[1] == 2002]
    assert len(to_admin) == 1, "повторний /start не спамить адміністратора"
    card, _, markup = to_admin[0]
    assert "Іван Петренко" in card and "@ivan_p" in card and "<code>2002</code>" in card
    buttons = [b["callback_data"] for row in json.loads(markup)["inline_keyboard"] for b in row]
    assert buttons == ["acc:viewer:2002", "acc:editor:2002", "acc:reject:2002"]
    assert "надіслано" in to_user[0][0] and "Очікуйте" in to_user[1][0]


def test_profile_text_is_html_escaped(db_path):
    service = make_service(db_path)
    service.handle_update(start_from({"id": 2002, "first_name": "<b>x</b> & y"}))
    card = [m for m in sent(service) if m[1] is None][0][0]
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in card


def test_start_from_member_gets_miniapp_button(db_path):
    register_request(db_path, NEWBIE, now=1)
    resolve_request(db_path, ADMIN, 2002, "viewer")
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    [(text, chat, markup)] = sent(service)
    assert chat == 2002 and "спостерігач" in text
    assert json.loads(markup)["inline_keyboard"][0][0]["web_app"]["url"].startswith("https://")


def test_start_in_admin_chat_is_greeting_not_request(db_path):
    service = make_service(db_path)
    service.handle_update(start_from({"id": ADMIN, "first_name": "Адмін"}))
    [(text, chat, _)] = sent(service)
    assert chat is None and "Radio 345" in text
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "telegram_access_requests" in tables:
            assert db.execute("SELECT COUNT(*) FROM telegram_access_requests").fetchone()[0] == 0


def test_start_in_group_is_ignored(db_path):
    service = make_service(db_path)
    update = start_from(NEWBIE)
    update["message"]["chat"] = {"id": -100500, "type": "group"}
    service.handle_update(update)
    service.send_text.assert_not_called()


def test_other_text_from_stranger_is_still_ignored(db_path):
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE, "/all"))
    service.send_text.assert_not_called()


def decision(data, actor=ADMIN, chat=ADMIN_CHAT):
    return {"update_id": 2, "callback_query": {
        "id": "cb", "data": data, "from": {"id": actor},
        "message": {"message_id": 77, "chat": {"id": int(chat)}},
    }}


def test_admin_button_grants_access_and_notifies_user(db_path):
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    service.send_text.reset_mock()

    service.handle_update(decision("acc:editor:2002"))

    assert (2002, "editor", 1) in users(db_path)
    edit = [c for c in service._request.call_args_list if c.args[1] == "editMessageText"][0]
    assert edit.kwargs["data"]["message_id"] == 77
    assert "reply_markup" not in edit.kwargs["data"], "кнопки прибираються після рішення"
    assert "Доступ надано" in edit.kwargs["data"]["text"] and "Іван Петренко" in edit.kwargs["data"]["text"]
    [(text, chat, markup)] = sent(service)
    assert chat == 2002 and "редактор" in text and "web_app" in markup


def test_admin_button_reject_notifies_user(db_path):
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    service.send_text.reset_mock()
    service.handle_update(decision("acc:reject:2002"))
    assert all(row[0] != 2002 for row in users(db_path))
    [(text, chat, _)] = sent(service)
    assert chat == 2002 and "не надав" in text


def test_decision_button_outside_admin_chat_is_ignored(db_path):
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    service.handle_update(decision("acc:editor:2002", actor=2002, chat="2002"))
    assert all(row[0] != 2002 for row in users(db_path))


def test_decision_by_non_admin_is_refused(db_path):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (3003, 'editor')")
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    service.handle_update(decision("acc:editor:2002", actor=3003))
    assert all(row[0] != 2002 for row in users(db_path))
    answer = [c for c in service._request.call_args_list if c.args[1] == "answerCallbackQuery"][-1]
    assert "адміністратор" in answer.kwargs["data"]["text"]


@pytest.mark.parametrize("data", ["acc:admin:2002", "acc:editor:abc", "acc:editor:0", "acc:editor"])
def test_malformed_decision_changes_nothing(db_path, data):
    service = make_service(db_path)
    service.handle_update(decision(data))
    assert users(db_path) == [(ADMIN, "admin", 1)]


# ---------- Рецензія: застарілі рішення, недоставлені картки, передача адмінки ----------

def test_old_approve_button_cannot_restore_revoked_access(db_path):
    register_request(db_path, NEWBIE, now=1)
    assert resolve_request(db_path, ADMIN, 2002, "editor")["status"] == "granted"
    with get_db(db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=2002")
    assert resolve_request(db_path, ADMIN, 2002, "editor")["status"] == "stale"
    assert resolve_request(db_path, ADMIN, 2002, None)["status"] == "stale"
    assert (2002, "editor", 0) in users(db_path), "пізніше відключення не скасовано"


def test_decision_without_any_request_changes_nothing(db_path):
    assert resolve_request(db_path, ADMIN, 2002, "viewer")["status"] == "stale"
    assert all(row[0] != 2002 for row in users(db_path))


def test_undelivered_card_is_resent_on_next_start(db_path):
    service = make_service(db_path)
    delivered = []
    service.send_text = Mock(side_effect=lambda text, *a, **k: (
        delivered.append(k.get("chat_id")) or len(delivered) > 1
        if "Telegram ID:" in text else True))
    service.handle_update(start_from(NEWBIE))
    first_reply = service.send_text.call_args.args[0]
    assert "Не вдалося передати" in first_reply

    service.handle_update(start_from(NEWBIE))
    assert len(delivered) == 2, "картку надіслано повторно без доби очікування"
    assert "надіслано" in service.send_text.call_args.args[0]
    assert register_request(db_path, NEWBIE)[0] == "pending"


def test_cards_go_to_every_active_admin_and_second_admin_can_decide(db_path):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (5005, 'admin')")
    service = make_service(db_path)
    service.handle_update(start_from(NEWBIE))
    cards = [c.kwargs.get("chat_id") for c in service.send_text.call_args_list if "Telegram ID:" in c.args[0]]
    assert cards == [None, 5005], "робочий чат (адмін 1001) і другий адміністратор"

    service.handle_update({"update_id": 9, "callback_query": {
        "id": "cb", "data": "acc:viewer:2002", "from": {"id": 5005},
        "message": {"message_id": 3, "chat": {"id": 5005, "type": "private"}},
    }})
    assert (2002, "viewer", 1) in users(db_path)


def test_disabled_env_owner_loses_telegram_privileges(db_path):
    with get_db(db_path) as db:
        db.execute("INSERT INTO telegram_users (user_id, role) VALUES (5005, 'admin')")
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=?", (ADMIN,))
    service = make_service(db_path)
    register_request(db_path, NEWBIE, now=1)

    service.handle_update(decision("acc:editor:2002", actor=ADMIN))
    assert all(row[0] != 2002 for row in users(db_path)), "відключений не може схвалювати"

    service._request.reset_mock()
    service.handle_update({"update_id": 10, "callback_query": {
        "id": "cb", "data": "del:db:5", "from": {"id": ADMIN},
        "message": {"message_id": 4, "chat": {"id": ADMIN, "type": "private"}},
    }})
    with sqlite3.connect(db_path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        if "telegram_phrase_messages" in tables:
            assert db.execute("SELECT COUNT(*) FROM telegram_phrase_messages").fetchone()[0] == 0
    assert [c.args[1] for c in service._request.call_args_list][0] == "deleteMessage", "лише власна копія"

    service.send_text.reset_mock()
    service.handle_update(start_from({"id": ADMIN, "first_name": "Власник"}))
    cards = [c.kwargs.get("chat_id") for c in service.send_text.call_args_list if "Telegram ID:" in c.args[0]]
    assert cards == [5005], "відключений власник .env просить доступ як усі"
