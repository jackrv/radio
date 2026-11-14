"""Mini App drafts and atomic corrections of events and temporary phrases."""

import re

from services.event_store import save_correction
from services.sqlite_connection import get_db
from services.telegram_phrase_store import load_phrase, update_phrase_text
from services.telegram_media_store import is_hidden, has_audio


class CorrectionConflict(Exception):
    """The event was modified after the editor loaded it."""


def event_id_from_fix_id(fix_id: str) -> int:
    if not isinstance(fix_id, str) or not re.fullmatch(
        r"db:[1-9][0-9]{0,17}", fix_id
    ):
        raise ValueError("Неприпустимий ідентифікатор виправлення")
    return int(fix_id[3:])


def load_edit_draft(db_path, fix_id: str):
    if is_hidden(db_path, fix_id):
        raise LookupError("Повідомлення видалено")
    if fix_id.startswith("mem:"):
        phrase = load_phrase(db_path, fix_id[4:])
        if phrase is None:
            raise LookupError("Фразу не знайдено або вона застаріла")
        return {"fix_id": fix_id, **phrase, "has_audio": has_audio(db_path, fix_id)}

    event_id = event_id_from_fix_id(fix_id)
    with get_db(db_path) as db:
        row = db.execute(
            "SELECT COALESCE(raw_text, text), text "
            "FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()

    if row is None:
        raise LookupError("Подію не знайдено")

    return {
        "fix_id": fix_id,
        "original_text": row[0],
        "current_text": row[1],
        "has_audio": has_audio(db_path, fix_id),
    }


def save_edit_draft(db_path, fix_id: str, expected_text: str,
                    corrected_text: str):
    memory_token = fix_id[4:] if fix_id.startswith("mem:") else None
    event_id = None if memory_token is not None else event_id_from_fix_id(fix_id)
    corrected = corrected_text.strip()
    if not corrected or len(corrected) > 5000:
        raise ValueError("Виправлений текст має бути непорожнім")

    with get_db(db_path) as db:
        # Serializes edits with other SQLite writers; protects against lost updates.
        db.execute("BEGIN IMMEDIATE")
        if is_hidden(db_path, fix_id, db=db):
            raise LookupError("Повідомлення видалено")
        if memory_token is not None:
            phrase = load_phrase(db_path, memory_token, db=db)
            if phrase is None:
                raise LookupError("Фразу не знайдено або вона застаріла")
            original = phrase["original_text"]
            current = phrase["current_text"]
        else:
            row = db.execute(
                "SELECT COALESCE(raw_text, text), text "
                "FROM radio_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Подію не знайдено")
            original, current = row

        if current != expected_text:
            raise CorrectionConflict("Текст змінився в іншому сеансі")

        # One transaction: word feedback + current text + rollback on errors.
        save_correction(
            original, corrected, event_id, db_path,
            source_id=fix_id if memory_token is not None else None,
            db=db,
        )
        if memory_token is not None:
            update_phrase_text(db, memory_token, corrected)

    return {
        "fix_id": fix_id,
        "corrected_text": corrected,
        "saved": True,
    }
