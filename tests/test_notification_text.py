"""Текст сповіщення: словникові слова за точним номером і рядок «Команди»."""

import logging
import sqlite3
from unittest.mock import Mock

from shared.callsigns import Callsigns, command_dictionary
from shared.analyzer import canonical_text, fmt_spoken, spoken_number
from core.event_processor import EventProcessor
from shared.event_store import save_event
from shared.miniapp_correction_editor import save_edit_draft
from services.telegram import TelegramService
from shared.telegram_outbox import build_caption
from shared.telegram_sync import remember_delivery, sync_corrections
from tests.test_miniapp_management import setup  # noqa: F401  (фікстура)

COMMANDS = command_dictionary(["злива 4242", "грім 022", "сніг"])
CALLSIGNS = Callsigns.from_lines(["дуб: 42 77", "клен"])
DICTIONARIES = (COMMANDS, CALLSIGNS)


def test_word_heard_with_exact_number_is_written_as_in_dictionary():
    text = "Злата 42 42, злата, кем 25 15, злата 42 42, прийом."
    assert canonical_text(text, DICTIONARIES) == "Злива 42 42, злата, кем 25 15, злива 42 42, прийом."


def test_callsign_with_exact_number_and_case():
    assert canonical_text("дубб 77, прийом", DICTIONARIES) == "дуб 77, прийом"
    assert canonical_text("Дубб, 42", DICTIONARIES) == "Дуб, 42"


def test_alias_rewrites_unknown_word_but_keeps_dictionary_word(monkeypatch):
    # Алиас «сніг -> дуб» (сніг — сама назва словника) і «дубок -> дуб».
    import shared.callsigns as callsigns
    monkeypatch.setattr(callsigns, "CALLSIGN_ALIASES", {"сніг": "дуб", "дубок": "дуб"})
    assert canonical_text("Сніг, 77, прийом", DICTIONARIES) == "Сніг, 77, прийом"
    assert canonical_text("Дубок 77, прийом", DICTIONARIES) == "Дуб 77, прийом"


def test_nothing_is_rewritten_without_exact_number():
    for text in ("злата, прийом",            # слово без номера
                 "дубб 99",                  # номер не зі словника
                 "Грім, 0,23, грім 023",     # неточний номер не підміняється й у шапці
                 "клін, сніх",               # позивні без номерів
                 "кем 25 15"):               # незрозумілий шматок
        assert canonical_text(text, DICTIONARIES) == text


def test_spoken_numbers():
    assert spoken_number("4242") == "42 42"
    assert spoken_number("022") == "0 22" and spoken_number("222") == "222"
    assert fmt_spoken(COMMANDS.find("злива 42 42, грім 022, сніг")) == "злива 42 42, грім 0 22, сніг"


def test_caption_has_commands_line_only_when_found():
    assert build_caption("Ефір", "", "", "текст", "злива 42 42") == "Подія: Ефір\nКоманди: злива 42 42\n\nтекст"
    assert build_caption("Ефір", "", "", "текст") == "Подія: Ефір\n\nтекст"


def processor(telegram, commands=COMMANDS, save=None):
    return EventProcessor(
        callsigns=CALLSIGNS, commands=commands, watch=Callsigns.from_lines([]), short_garbage=set(),
        save_event=save or Mock(return_value=7), telegram_service=telegram, fresh=lambda _: True,
        select_recipients=lambda wants: [{"chat_id": 1, "role": "admin", "mode": "all"}],
        db_path="unused-test.db", log_callsigns_only=True, log=Mock(),
    )


def test_event_and_notification_get_dictionary_words_heard_text_is_kept_as_raw():
    telegram, save = Mock(), Mock(return_value=7)
    heard = "Злата 42 42, злата, кем 25 15, прийом."
    processor(telegram, save=save).process(heard, b"\0\0")
    assert save.call_args.args[4] == "ЗЛИВА 42 42, злата, кем 25 15, прийом"
    assert save.call_args.kwargs["raw_text"] == heard
    assert telegram.notify.call_args.args[3] == "ЗЛИВА 42 42, злата, кем 25 15, прийом"
    assert telegram.notify.call_args.kwargs["commands_str"] == "злива 42 42"


def test_notification_is_sent_even_if_autocorrection_fails():
    telegram, save = Mock(), Mock(return_value=7)
    broken = Mock(wraps=COMMANDS)
    broken.matches.side_effect = RuntimeError("synthetic")
    processor(telegram, commands=broken, save=save).process("злива 42 42", b"\0\0")
    telegram.notify.assert_called_once()
    assert telegram.notify.call_args.args[3] == "злива 42 42"
    assert telegram.notify.call_args.kwargs["commands_str"] == "злива 42 42"
    assert save.call_args.args[4] == "злива 42 42" and "raw_text" not in save.call_args.kwargs


def test_corrected_caption_has_commands_line(setup):  # noqa: F811
    _, path = setup
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries")
        db.executemany("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES (?,?,?)",
                       [("callsigns", 1, "клен"), ("commands", 1, "злива 4242")])
    original, corrected = "Злата 42, 42", "Злива 42 42, прийом"
    fix = f'db:{save_event("", "злива 4242", "", "", original, path, raw_text=original)}'
    remember_delivery(path, fix, {"message_id": 1, "chat": {"id": 1001}, "voice": {"file_id": "fake"}}, "voice")
    save_edit_draft(path, fix, original, corrected)
    bot = TelegramService("test", 16000, path, logging.getLogger("test"))
    bot._request = Mock(return_value=Mock(status_code=200, json=Mock(return_value={"ok": True})))
    assert sync_corrections(bot) == 1
    assert bot._request.call_args.kwargs["data"]["caption"] == (
        "✏️ Виправлено оператором\nПодія: Ефір\nКоманди: злива 42 42\n\n" + corrected)
