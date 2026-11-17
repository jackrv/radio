"""Видалення надісланої фрази в Telegram в усіх отримувачів (з «Аудіоархіву» панелі)."""
import time

from services.event_store import save_event
from services.miniapp_correction_editor import save_edit_draft
from services.telegram_media_store import is_hidden
from services.telegram_recall import deletable, process_deletions, request_delete, state
from services.telegram_sync import remember_delivery, sync_corrections
from tests.test_miniapp_management import setup  # noqa: F401 — фікстура
from tests.test_telegram_sync import message, response, service


def test_every_copy_is_deleted_by_the_bot_and_the_phrase_is_hidden(setup):
    _, path = setup
    fix = f'db:{save_event("", "", "", "", "Вишня 12, прийом", path)}'
    for chat in (1001, 2002, 3003):
        remember_delivery(path, fix, message(chat, chat + 1), "voice")
    assert state(path, [fix])[fix] == {"copies": 3, "pending": 0, "requested": False, "hidden": False}
    assert request_delete(path, fix) == 3
    assert is_hidden(path, fix), "фраза зникає з журналу й Mini App одразу"
    assert state(path, [fix])[fix]["pending"] == 3
    bot = service(path)
    bot._request.side_effect = [response(), response({"ok": False, "description": "Bad Request: message to delete not found"}),
                                response({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 5}}, 429)]
    assert process_deletions(bot) == 3
    chats = sorted(c.kwargs["data"]["chat_id"] for c in bot._request.call_args_list if c.args[1] == "deleteMessage")
    assert chats == [1001, 2002, 3003]
    assert state(path, [fix])[fix]["pending"] == 1, "збій — повтор пізніше"
    bot._request.side_effect = None
    bot._request.return_value = response()
    assert process_deletions(bot, now=time.time() + 60) == 1
    assert state(path, [fix])[fix] == {"copies": 0, "pending": 0, "requested": True, "hidden": True}
    assert request_delete(path, fix) == 0, "повторний запит нічого не надсилає"


def test_pending_deletion_is_not_edited_by_correction_sync(setup):
    _, path = setup
    fix = f'db:{save_event("", "", "", "", "Вира 68", path)}'
    remember_delivery(path, fix, message(1001), "voice")
    save_edit_draft(path, fix, "Вира 68", "Віра 68")
    request_delete(path, fix)
    bot = service(path)
    assert sync_corrections(bot) == 0
    assert state(path, [fix])[fix]["pending"] == 1, "копія лишається в черзі на видалення"


def test_only_within_telegram_48_hour_window():
    now = 1_000_000
    assert deletable(now - 47 * 3600, now)
    assert not deletable(now - 48 * 3600 + 60, now)
