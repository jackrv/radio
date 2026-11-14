import sqlite3
import threading
from unittest.mock import Mock

import services.telegram as telegram_module

from callsigns import Callsigns
from core.event_processor import EventProcessor
from database import init_db as init_dictionary_db
from services.event_store import init_db, save_event
from services.telegram import TelegramService


def test_full_telegram_correction_flow(tmp_path, monkeypatch):
    path = str(tmp_path / "events.db")

    init_db(path)
    init_dictionary_db(path)

    with sqlite3.connect(path) as db:
        db.executemany(
            """
            INSERT INTO dictionary_entries
                (dictionary_type, line_number, raw_line)
            VALUES (?, ?, ?)
            """,
            [
                ("callsigns", 1, "липка: 700"),
                ("callsigns", 2, "наган: 110 112"),
                ("commands", 1, "прийом"),
            ],
        )

    callsigns = Callsigns.from_lines([
        "липка: 700",
        "наган: 110 112",
    ])

    commands = Callsigns.from_lines(["прийом"])
    watch = Callsigns.from_lines([])

    pending = {}
    lock = threading.Lock()

    service = TelegramService(
        bot_token="fake_token",
        chat_id="123",
        sample_rate=16000,
        db_path=path,
        log=Mock(),
        state_lock=lock,
        waiting_for_correction=pending,
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )

    service.notify = Mock()
    service.send_text = Mock()

    monkeypatch.setattr(
        telegram_module.requests,
        "post",
        Mock(),
    )

    processor = EventProcessor(
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        short_garbage=set(),
        save_event=save_event,
        telegram_service=service,
        fresh=lambda _: True,
        state_lock=lock,
        get_notify_mode=lambda: "all",
        db_path=path,
        log_callsigns_only=True,
        log=Mock(),
    )

    raw = "Ліпка 700, я Наган 110, прийом"
    processed = "Липка 700, я Наган 110, прийом"
    corrected = "Липка 700, я Наган 112, прийом"

    # 1. Сохраняем распознанное событие.
    processor.process(
        processed,
        b"audio",
        raw_text=raw,
    )

    with sqlite3.connect(path) as db:
        event_id, text, raw_text = db.execute("""
            SELECT id, text, raw_text
            FROM radio_events
        """).fetchone()

    assert text == processed
    assert raw_text == raw

    # 2. Получаем кнопку исправления события.
    service.notify.assert_called_once()

    fix_id = service.notify.call_args.kwargs["fix_id"]
    assert fix_id == f"db:{event_id}"

    # 3. Пользователь нажимает «Исправить».
    service.handle_update({
        "callback_query": {
            "id": "callback_1",
            "data": f"fix_{fix_id}",
            "message": {
                "chat": {"id": 123},
                "message_id": 50,
            },
        }
    })

    assert pending["123"]["raw_text"] == raw
    assert pending["123"]["event_id"] == event_id

    # 4. Пользователь присылает правильный текст.
    service.handle_update({
        "message": {
            "chat": {"id": 123},
            "text": corrected,
        }
    })

    assert "123" not in pending

    # 5. Проверяем все связанные поля.
    with sqlite3.connect(path) as db:
        row = db.execute("""
            SELECT text, raw_text, callsigns,
                   commands, recipient, sender
            FROM radio_events
            WHERE id = ?
        """, (event_id,)).fetchone()

    assert row == (
        corrected,
        raw,
        "липка 700, наган 112",
        "прийом",
        "липка 700",
        "наган 112",
    )
