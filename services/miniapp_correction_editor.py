"""Mini App edit draft and atomic correction of a persisted radio event."""

import re

from services.event_store import save_correction
from services.sqlite_connection import get_db


class CorrectionConflict(Exception):
    """The event was modified after the editor loaded it."""


def event_id_from_fix_id(fix_id: str) -> int:
    if not isinstance(fix_id, str) or not re.fullmatch(
        r"db:[1-9][0-9]{0,17}", fix_id
    ):
        raise ValueError("Недопустимый идентификатор исправления")
    return int(fix_id[3:])


def load_edit_draft(db_path, fix_id: str):
    event_id = event_id_from_fix_id(fix_id)
    with get_db(db_path) as db:
        row = db.execute(
            "SELECT COALESCE(raw_text, text), text "
            "FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()

    if row is None:
        raise LookupError("Событие не найдено")

    return {
        "fix_id": fix_id,
        "original_text": row[0],
        "current_text": row[1],
    }


def save_edit_draft(db_path, fix_id: str, expected_text: str,
                    corrected_text: str):
    event_id = event_id_from_fix_id(fix_id)
    corrected = corrected_text.strip()
    if not corrected or len(corrected) > 5000:
        raise ValueError("Исправленный текст должен быть непустым")

    with get_db(db_path) as db:
        # Serializes edits with other SQLite writers; protects against lost updates.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT COALESCE(raw_text, text), text "
            "FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()

        if row is None:
            raise LookupError("Событие не найдено")
        original, current = row
        if current != expected_text:
            raise CorrectionConflict("Текст изменился в другой сессии")

        # Uses the existing transaction-aware event and word-learning service.
        save_correction(original, corrected, event_id, db_path, db=db)

    return {
        "fix_id": fix_id,
        "corrected_text": corrected,
        "saved": True,
    }
