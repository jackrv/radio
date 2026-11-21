"""Побажання боту: лише тимчасові файли/SQLite та підмінені відповіді Telegram."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from services.telegram import TelegramService
from services import telegram_wishlist as module
from services.telegram_wishlist import WishlistInbox
from services.user_notify_store import load_user_mode
from services.user_store import bootstrap_admin, create_user, init_users_db


@pytest.fixture
def bot(tmp_path):
    path = tmp_path / "events.db"
    init_users_db(path)
    bootstrap_admin(path, 1001)
    create_user(path, 1001, 2002, role="editor")
    create_user(path, 1001, 3003, role="viewer")
    return make_bot(path)


def make_bot(path):
    service = TelegramService("test", 16000, path, Mock())
    service.send_text = Mock(return_value=True)
    return service


def update(text, message_id=10, *, actor=1001, chat=None, kind="private"):
    return {"update_id": message_id + 100, "message": {
        "message_id": message_id, "text": text, "from": {"id": actor},
        "chat": {"id": actor if chat is None else chat, "type": kind},
    }}


@pytest.mark.parametrize("command", ["Идея", "Добавить", "Ідея", "Додати", "/idea", "/add", "ИДЕЯ", "дОбАвИтЬ"])
def test_prompt_survives_restart_and_saves_next_text(bot, tmp_path, command):
    bot.handle_update(update(command))
    assert "наступним" in bot.send_text.call_args.args[0]
    recovered = make_bot(bot.db_path)
    recovered.handle_update(update("Зробити зручніший пошук", 11))
    text = (tmp_path / ".wishlist").read_text()
    assert text.count("Зробити зручніший пошук") == 1
    assert "збережено" in recovered.send_text.call_args.args[0]
    assert recovered.send_text.call_args.kwargs == {"chat_id": 1001, "parse_mode": None}
    assert not (os.stat(tmp_path / ".wishlist").st_mode & 0o077)


@pytest.mark.parametrize("prefix", ["Идея: ", "Добавить ", "Ідея:\n", "/idea ", "/add: "])
def test_inline_and_replayed_message_do_not_duplicate(bot, tmp_path, prefix):
    (tmp_path / ".wishlist").write_text("# Уже є\n\nНе прибирати\n")
    message = update(prefix + "Додати приклад\nДругий рядок")
    bot.handle_update(message)
    recovered = make_bot(bot.db_path)
    recovered.send_text.return_value = False  # Втрачена відповідь не відкочує файл.
    recovered.handle_update(message)
    recovered.handle_update(message)
    text = (tmp_path / ".wishlist").read_text()
    assert text.startswith("# Уже є\n\nНе прибирати")
    assert text.count("Додати приклад\nДругий рядок") == 1
    assert len(list(tmp_path.glob(".local-wishlist-*"))) == 0


@pytest.mark.parametrize("actor,chat,kind", [(2002,None,"private"), (3003,None,"private"),
    (4004,None,"private"), (1001,-99,"group"), (1001,2002,"private")])
def test_only_current_admin_in_own_private_chat_can_write(bot, tmp_path, actor, chat, kind):
    bot.handle_update(update("Идея: Не зберігати", actor=actor, chat=chat, kind=kind))
    assert not (tmp_path / ".wishlist").exists()
    assert not (tmp_path / ".local-telegram-wishlist").exists()
    bot.send_text.assert_not_called()


def test_revoked_admin_cannot_complete_pending_idea(bot, tmp_path):
    bot.handle_update(update("Идея"))
    with sqlite3.connect(bot.db_path) as db:
        db.execute("UPDATE telegram_users SET enabled=0 WHERE user_id=1001")
    bot.handle_update(update("Не зберігати", 11))
    assert not (tmp_path / ".wishlist").exists()


def test_database_failure_never_grants_wishlist_access(bot, tmp_path):
    bot._user_state = Mock(return_value=None)
    bot.handle_update(update("Идея: Не зберігати"))
    assert not (tmp_path / ".wishlist").exists()


def test_only_admin_start_help_mentions_ideas(bot):
    assert "/idea" in bot._mode_hint(1001, "admin")
    assert "/idea" not in bot._mode_hint(2002, "editor")
    assert "/idea" not in bot._mode_hint(3003, "viewer")


def test_cancel_and_mode_commands_do_not_become_ideas(bot, tmp_path):
    bot.handle_update(update("Идея"))
    bot.send_status = Mock()
    bot.clean_command = Mock()
    bot.handle_update(update("/off", 11))
    assert load_user_mode(bot.db_path, 1001) == ("admin", "off")
    bot.handle_update(update("/cancel", 12))
    bot.handle_update(update("Звичайне повідомлення", 13))
    assert not (tmp_path / ".wishlist").exists()
    bot.handle_update(update("Идея: Явне побажання", 14))
    assert "Явне побажання" in (tmp_path / ".wishlist").read_text()


def test_timeout_and_unrelated_text_are_ignored(tmp_path):
    inbox = WishlistInbox(tmp_path)
    assert inbox.accept(1001, 1, "Звичайне повідомлення", now=0) is None
    assert not inbox.directory.exists()
    assert inbox.accept(1001, 2, "Идея", now=0) == "waiting"
    assert inbox.accept(1001, 3, "Запізнілий текст", now=module.WAIT_SECONDS) is None
    assert not inbox.path.exists()


def test_two_admins_have_separate_pending_texts_and_concurrent_appends(tmp_path):
    inbox = WishlistInbox(tmp_path)
    inbox.accept(1001, 1, "Идея")
    inbox.accept(2002, 1, "Добавить")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda actor: inbox.accept(actor, 2, f"Синтетична ідея {actor}"), [1001,2002]))
    assert results == ["saved", "saved"]
    assert inbox.path.read_text().count("## Ідея") == 2
    assert json.loads(inbox.state_path.read_text()) == {}


def test_failed_atomic_write_preserves_original_and_pending_dialog(bot, tmp_path, monkeypatch):
    bot.handle_update(update("Идея"))
    (tmp_path / ".wishlist").write_text("Не втратити цей текст\n")
    original = module.os.replace
    def fail(source, target):
        if target == tmp_path / ".wishlist":
            raise OSError("synthetic storage failure")
        return original(source, target)
    monkeypatch.setattr(module.os, "replace", fail)
    bot.handle_update(update("Секретний синтетичний текст", 11))
    assert (tmp_path / ".wishlist").read_text() == "Не втратити цей текст\n"
    assert "Не вдалося" in bot.send_text.call_args.args[0]
    assert "Секретний" not in str(bot.log.mock_calls)
    monkeypatch.setattr(module.os, "replace", original)
    bot.handle_update(update("Секретний синтетичний текст", 11))
    assert (tmp_path / ".wishlist").read_text().count("Секретний") == 1


def test_saved_file_with_uncleared_state_does_not_capture_unrelated_next_message(tmp_path, monkeypatch):
    inbox = WishlistInbox(tmp_path)
    inbox.accept(1001, 1, "Идея")
    original = module._replace
    def fail(path, text):
        if path == inbox.state_path:
            raise OSError("synthetic state failure")
        return original(path, text)
    monkeypatch.setattr(module, "_replace", fail)
    assert inbox.accept(1001, 2, "Збережене побажання") == "saved"
    monkeypatch.setattr(module, "_replace", original)
    assert inbox.accept(1001, 3, "Це не побажання") is None
    assert "Це не побажання" not in inbox.path.read_text()
    assert inbox.accept(1001, 2, "Збережене побажання") == "saved"
    assert inbox.path.read_text().count("Збережене побажання") == 1


@pytest.mark.parametrize("target", [".wishlist", ".local-telegram-wishlist"])
def test_symlinks_are_rejected_without_touching_target(bot, tmp_path, target):
    outside = tmp_path / "keep"
    outside.write_text("Не змінювати")
    (tmp_path / target).symlink_to(outside)
    bot.handle_update(update("Идея: Не записувати"))
    assert outside.read_text() == "Не змінювати"
    assert "Не вдалося" in bot.send_text.call_args.args[0]


@pytest.mark.parametrize("state", ["not json", "[]", '{"1001":{}}'])
def test_corrupted_state_is_not_silently_overwritten(bot, tmp_path, state):
    directory = tmp_path / ".local-telegram-wishlist"
    directory.mkdir()
    (directory / "state.json").write_text(state)
    bot.handle_update(update("Идея: Текст"))
    assert (directory / "state.json").read_text() == state
    assert not (tmp_path / ".wishlist").exists()
    assert "Не вдалося" in bot.send_text.call_args.args[0]


def test_oversized_idea_keeps_waiting_and_never_saves_partial_text(tmp_path):
    inbox = WishlistInbox(tmp_path)
    inbox.accept(1001, 1, "Идея")
    assert inbox.accept(1001, 2, "а" * (module.MAX_TEXT + 1)) == "invalid"
    assert not inbox.path.exists()
    assert inbox.accept(1001, 3, "Коротка ідея") == "saved"
