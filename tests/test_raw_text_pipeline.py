import sqlite3
import threading
from unittest.mock import Mock

import core.transcriber as transcriber_module
from core.event_processor import EventProcessor
from core.transcriber import Transcriber
from services.event_store import init_db, save_event


def test_transcriber_passes_original_text(monkeypatch):
    monkeypatch.setattr(
        transcriber_module,
        "looks_like_garbage",
        lambda *_: False,
    )

    recognizer = Mock()
    recognizer.extract_text.return_value = "Ліпка 700, прийом"

    processor = Mock()

    worker = Transcriber(
        segments=Mock(),
        recognizer=recognizer,
        process_text=lambda _: "Липка 700, прийом",
        event_processor=processor,
        sample_rate=16000,
        min_rms=0.02,
        normalize_audio=True,
        debug_rms=False,
        blacklist=(),
        log=Mock(),
    )

    pcm = (10000).to_bytes(
        2, "little", signed=True
    ) * 16000

    worker.process_segment(pcm)

    processor.process.assert_called_once_with(
        "Липка 700, прийом",
        pcm,
        raw_text="Ліпка 700, прийом",
    )


def make_processor(db_path):

    callsigns = Mock()
    callsigns.find.return_value = []

    commands = Mock()

    # fmt() ожидает тройку: (название, номер, позиция).
    commands.find.return_value = [("пуск", "", 0)]

    watch = Mock()
    watch.find.return_value = []

    processor = EventProcessor(
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        short_garbage=set(),
        save_event=save_event,
        telegram_service=Mock(),
        fresh=lambda _: True,
        state_lock=threading.Lock(),
        get_notify_mode=lambda: "watch",
        db_path=str(db_path),
        log_callsigns_only=False,
        log=Mock(),
    )

    return processor


def test_stores_original_and_corrected_text(tmp_path, monkeypatch):
    import core.event_processor as ep

    monkeypatch.setattr(
        ep, "split_roles", lambda *_: ([], [])
    )

    db_path = tmp_path / "events.db"
    init_db(str(db_path))

    processor = make_processor(db_path)

    processor.process(
        "Липка 700, прийом",
        b"audio",
        raw_text="Ліпка 700, прийом",
    )

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT text, raw_text FROM radio_events"
        ).fetchall()

    assert rows == [
        ("Липка 700, прийом", "Ліпка 700, прийом")
    ]


def test_legacy_call_without_raw_text(tmp_path, monkeypatch):
    import core.event_processor as ep

    monkeypatch.setattr(
        ep, "split_roles", lambda *_: ([], [])
    )

    db_path = tmp_path / "events.db"
    init_db(str(db_path))

    processor = make_processor(db_path)
    processor.process("Липка 700", b"audio")

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT text, raw_text FROM radio_events"
        ).fetchall()

    assert rows == [("Липка 700", None)]
