import threading
from unittest.mock import Mock

from core.event_processor import EventProcessor
from services.event_store import (
    init_db,
    save_event,
    resolve_event_text,
)


def test_resolve_returns_whisper_original(tmp_path):
    path = str(tmp_path / "events.db")
    init_db(path)

    event_id = save_event(
        "липка 700", "", "", "",
        "Липка 700, прийом",
        path,
        raw_text="Ліпка 700, прийом",
    )

    assert resolve_event_text(event_id, path) == (
        "Ліпка 700, прийом"
    )

    old_id = save_event(
        "наган 110", "", "", "",
        "Наган 110", path,
    )

    assert resolve_event_text(old_id, path) == "Наган 110"


def test_event_processor_registers_whisper_original():
    callsigns = Mock()
    callsigns.find.return_value = [("липка", "700", 0)]

    commands = Mock()
    commands.find.return_value = []

    watch = Mock()
    watch.find.return_value = []

    telegram = Mock()
    telegram.register_phrase_for_fix.return_value = "db:21"

    processor = EventProcessor(
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        short_garbage=set(),
        save_event=Mock(return_value=21),
        telegram_service=telegram,
        fresh=lambda _: True,
        state_lock=threading.Lock(),
        get_notify_mode=lambda: "all",
        db_path="unused-test.db",
        log_callsigns_only=True,
        log=Mock(),
    )

    processor.process(
        "Липка 700, прийом",
        b"audio",
        raw_text="Ліпка 700, прийом",
    )

    telegram.register_phrase_for_fix.assert_called_once_with(
        "Ліпка 700, прийом", 21
    )

    assert telegram.notify.call_args.args[3] == (
        "Липка 700, прийом"
    )
