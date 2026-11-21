"""Пошук фраз журналу для редактора Mini App."""
import time
from services.sqlite_connection import get_db
from services.telegram_phrase_store import ensure_table, RETENTION_SECONDS
from services.telegram_media_store import ensure_table as ensure_media_table


def list_phrases(db_path, query="", offset=0, limit=20):
    # Коротка окрема транзакція очищення: пошук нижче не тримає блокування запису,
    # тож перегляд журналу не затримує збереження нових подій приймачем.
    with get_db(db_path) as db:
        ensure_table(db)
        ensure_media_table(db)
        from services.telegram_sync import ensure_tables as ensure_deliveries
        ensure_deliveries(db)
        db.execute("DELETE FROM telegram_recent_phrases WHERE created_at <= ?",
                   (int(time.time()) - RETENTION_SECONDS,))
        db.execute("""DELETE FROM telegram_phrase_messages WHERE fix_id LIKE 'mem:%' AND state='visible'
            AND NOT EXISTS (SELECT 1 FROM telegram_recent_phrases p WHERE 'mem:' || p.token=fix_id)
            AND NOT EXISTS (SELECT 1 FROM telegram_deliveries d
                WHERE d.fix_id=telegram_phrase_messages.fix_id AND d.unavailable=0)""")
    with get_db(db_path) as db:
        db.create_function("casefold", 1, lambda text: (text or "").casefold())
        rows = db.execute("""
            SELECT * FROM (
                SELECT 'db:' || id AS fix_id, COALESCE(raw_text, text) AS original_text,
                       text AS current_text, ts AS created_at
                FROM radio_events
                UNION ALL
                SELECT 'mem:' || token, original_text, current_text,
                       datetime(created_at, 'unixepoch') FROM telegram_recent_phrases
            ) AS phrases WHERE (instr(casefold(original_text), ?) > 0 OR instr(casefold(current_text), ?) > 0)
              AND NOT EXISTS (SELECT 1 FROM telegram_phrase_messages m
                  WHERE m.fix_id=phrases.fix_id AND m.state<>'visible')
            ORDER BY created_at DESC, fix_id DESC LIMIT ? OFFSET ?
        """, (query.casefold(), query.casefold(), limit + 1, offset)).fetchall()
        media = {row[0]: bool(db.execute(
            "SELECT 1 FROM telegram_phrase_messages WHERE fix_id=? AND state='visible' AND voice_file_id IS NOT NULL",
            (row[0],)).fetchone()) for row in rows[:limit]}
    return {"items": [{**dict(zip(("fix_id", "original_text", "current_text", "created_at"), r)),
                       "has_audio": bool(media.get(r[0]))} for r in rows[:limit]], "has_more": len(rows) > limit}
