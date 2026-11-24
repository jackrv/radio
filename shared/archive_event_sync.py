"""Синхронізація еталона архіву з подією та чергою Telegram — без залежності від HTTP."""
import logging
import re
from shared.sqlite_connection import get_db


def sync_event_from_reference(db_path, record, admin):
    """Еталон мовлення стає текстом пов'язаної події: оновлюються журнал, підпис у
    Telegram і навчання правил — так само, як після правки в Mini App.

    Повертає None (немає події), "same", "updated", "hidden" або "failed".
    Збій не скасовує збережений еталон.
    """
    fix_id = (record.get("data") or {}).get("fix_id") or ""
    reference = (record.get("reference") or "").strip()
    if record.get("label") not in ("speech", "unclear") or not reference or not re.fullmatch(r"db:[1-9][0-9]{0,17}", fix_id):
        return None
    from shared.miniapp_correction_editor import CorrectionConflict, save_edit_draft
    from shared.telegram_media_store import is_hidden
    try:
        if is_hidden(db_path, fix_id):
            return "hidden"
        for _ in range(2):   # подію могли змінити між читанням і записом — одна повторна спроба
            with get_db(db_path) as db:
                row = db.execute("SELECT text FROM radio_events WHERE id=?", (int(fix_id[3:]),)).fetchone()
            if row is None:
                return None
            if row[0] == reference:
                return "same"
            try:
                save_edit_draft(db_path, fix_id, row[0], reference, actor_id=admin)
                return "updated"
            except CorrectionConflict:
                continue
        return "failed"
    except Exception:
        logging.getLogger("radio").exception("Не вдалося перенести еталон у подію %s", fix_id)
        return "failed"
