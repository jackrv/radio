import logging

import pytest

from services.miniapp_correction_editor import load_edit_draft, save_edit_draft
from services.telegram import TelegramService
from services.event_store import init_db
from services.sqlite_connection import get_db


@pytest.fixture
def setup_service(tmp_path):
    path = tmp_path / "telegram_learning.db"
    init_db(path)

    service = TelegramService(
        bot_token="test-token",
        sample_rate=16000,
        db_path=str(path),
        log=logging.getLogger("test_word_learning"),
    )

    service.send_text = lambda *args, **kwargs: None
    return service, path


def correct_message(service, fix_id, text):
    """Виправлення через редактор Mini App (той самий шлях збереження)."""
    path = service.db_path
    current = load_edit_draft(path, fix_id)["current_text"]
    save_edit_draft(path, fix_id, current, text)


def get_rules(path):
    with get_db(path) as db:
        return db.execute("""
            SELECT original, corrected, confirmations
            FROM word_corrections
            ORDER BY original, corrected
        """).fetchall()


def test_mem_messages_accumulate_confirmations(setup_service):
    service, path = setup_service

    first = service.register_phrase_for_fix(
        "На ган 110", None
    )

    second = service.register_phrase_for_fix(
        "На ган 112", None
    )

    correct_message(service, first, "Наган 110")
    correct_message(service, second, "Наган 112")

    assert get_rules(path) == [
        ("на ган", "наган", 2)
    ]


def test_repeated_mem_correction_not_counted_twice(setup_service):
    service, path = setup_service

    fix_id = service.register_phrase_for_fix(
        "Лира 68", None
    )

    correct_message(service, fix_id, "Ліра 68")
    correct_message(service, fix_id, "Ліра 68")

    assert get_rules(path) == [
        ("лира", "ліра", 1)
    ]


def test_revising_mem_correction_replaces_vote(setup_service):
    service, path = setup_service

    fix_id = service.register_phrase_for_fix(
        "Лира 68", None
    )

    correct_message(service, fix_id, "Ліра 68")
    correct_message(service, fix_id, "Ліро 68")

    assert get_rules(path) == [
        ("лира", "ліра", 0),
        ("лира", "ліро", 1),
    ]
