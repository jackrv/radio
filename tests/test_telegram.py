import json
import logging
import threading

from services.telegram import TelegramService


def make_service():
    return TelegramService(
        bot_token="",
        chat_id="",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test"),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )


def test_keyboard_without_fix_id():
    service = make_service()

    keyboard = json.loads(service.get_keyboard())

    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "del"
    assert len(keyboard["inline_keyboard"][0]) == 1


def test_keyboard_with_fix_id():
    service = make_service()

    keyboard = json.loads(service.get_keyboard("mem:12345"))

    buttons = keyboard["inline_keyboard"][0]

    assert buttons[0]["callback_data"] == "del"
    assert buttons[1]["callback_data"] == "fix_mem:12345"


def test_register_and_resolve_memory_phrase():
    service = make_service()

    fix_id = service.register_phrase_for_fix("тестовая фраза", None)

    text, event_id = service.resolve_phrase_by_fix_id(fix_id)

    assert fix_id.startswith("mem:")
    assert text == "тестовая фраза"
    assert event_id is None


def test_recent_phrases_limit():
    service = make_service()
    service.recent_phrases_max = 3

    for i in range(5):
        service.register_phrase_for_fix(f"фраза {i}", None)

    assert len(service.recent_phrases) == 3
    assert "фраза 0" not in service.recent_phrases.values()
    assert "фраза 4" in service.recent_phrases.values()


def test_notification_modes():
    modes = {"value": "all"}

    service = TelegramService(
        bot_token="",
        chat_id="123",
        sample_rate=16000,
        db_path="unused_test.db",
        log=logging.getLogger("test"),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: modes["value"],
        set_notify_mode=lambda mode: modes.update(value=mode),
    )

    service.send_text = lambda text, reply_markup=None, parse_mode="HTML": None

    for command, expected in [
        ("/watch", "watch"),
        ("/commands", "commands"),
        ("/all", "all"),
    ]:
        service.handle_update({
            "message": {
                "chat": {"id": 123},
                "text": command,
            }
        })
        assert modes["value"] == expected


def test_foreign_chat_is_ignored():
    service = make_service()
    service.chat_id = "123"

    service.handle_update({
        "message": {
            "chat": {"id": 456},
            "text": "/watch",
        }
    })

    assert service.waiting_for_correction == {}





def test_fix_callback_starts_correction():
    from unittest.mock import patch

    service = make_service()
    service.chat_id = "123"
    service.send_text = lambda *args, **kwargs: None

    fix_id = service.register_phrase_for_fix("исходная фраза", None)

    update = {
        "callback_query": {
            "id": "callback-1",
            "data": f"fix_{fix_id}",
            "message": {
                "message_id": 10,
                "chat": {"id": 123},
            },
        }
    }

    with patch("services.telegram.requests.post"):
        service.handle_update(update)

    assert service.waiting_for_correction["123"]["raw_text"] == "исходная фраза"
    assert service.waiting_for_correction["123"]["event_id"] is None


def test_corrected_text_is_saved():
    from unittest.mock import patch

    service = make_service()
    service.chat_id = "123"

    service.waiting_for_correction["123"] = {
        "event_id": None,
        "raw_text": "исходная фраза",
    }

    service.send_text = lambda *args, **kwargs: None

    update = {
        "message": {
            "chat": {"id": 123},
            "text": "правильная фраза",
        }
    }

    with patch(
        "services.telegram.save_correction"
    ) as mock_save:
        service.handle_update(update)

    mock_save.assert_called_once_with(
        "исходная фраза",
        "правильная фраза",
        None,
        "unused_test.db",
    )

    assert "123" not in service.waiting_for_correction


def test_fix_callback_shows_copy_text_button():
    from unittest.mock import patch

    service = make_service()
    service.chat_id = "123"

    captured = {}

    def capture_send_text(text, reply_markup=None, parse_mode="HTML"):
        captured["text"] = text
        captured["keyboard"] = json.loads(reply_markup)

    service.send_text = capture_send_text

    original = "исходная фраза"
    fix_id = service.register_phrase_for_fix(original, None)

    update = {
        "callback_query": {
            "id": "callback-copy",
            "data": f"fix_{fix_id}",
            "message": {
                "message_id": 10,
                "chat": {"id": 123},
            },
        }
    }

    with patch("services.telegram.requests.post"):
        service.handle_update(update)

    buttons = captured["keyboard"]["inline_keyboard"][0]
    copy_button = next(
        button for button in buttons
        if button.get("copy_text")
    )

    assert copy_button["text"] == "📋 Скопировать текст"
    assert copy_button["copy_text"]["text"] == original
    assert "Отмена" in buttons[-1]["text"]


def test_empty_message_keeps_pending_correction():
    from unittest.mock import patch

    service = make_service()
    service.chat_id = "123"

    pending = {
        "event_id": 42,
        "raw_text": "исходная фраза",
    }
    service.waiting_for_correction["123"] = pending

    update = {
        "message": {
            "chat": {"id": 123},
        }
    }

    with patch("services.telegram.save_correction") as mock_save:
        service.handle_update(update)

    assert service.waiting_for_correction["123"] == pending
    mock_save.assert_not_called()


def test_correction_text_is_html_escaped():
    from unittest.mock import patch

    service = make_service()
    service.chat_id = "123"

    original = "исходный <текст> & данные"
    corrected = "исправленный <текст> & данные"

    service.waiting_for_correction["123"] = {
        "event_id": None,
        "raw_text": original,
    }

    sent = []
    service.send_text = lambda text, **kwargs: sent.append(text)

    update = {
        "message": {
            "chat": {"id": 123},
            "text": corrected,
        }
    }

    with patch("services.telegram.save_correction") as mock_save:
        service.handle_update(update)

    mock_save.assert_called_once_with(
        original,
        corrected,
        None,
        "unused_test.db",
    )

    assert "&lt;текст&gt; &amp; данные" in sent[0]
    assert sent[0].count("&lt;") == 2
    assert sent[0].count("&gt;") == 2
    assert sent[0].count("&amp;") == 2
