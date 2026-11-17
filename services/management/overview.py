"""Зведення для Mini App і панелі: правки, правила, словники."""
from services.sqlite_connection import get_db
from services.management.learning import learning_overview


def overview(db_path):
    with get_db(db_path) as db:
        events = db.execute("SELECT COUNT(*), MAX(ts) FROM radio_events").fetchone()
        from services.telegram_sync import ensure_tables
        ensure_tables(db)
        # Виправлені оператором фрази (а не правила й не голоси).
        corrections = db.execute("SELECT COUNT(*) FROM (SELECT source_id FROM word_correction_votes UNION SELECT fix_id FROM correction_sources)").fetchone()[0]
        total_rules = db.execute("SELECT COUNT(*) FROM word_corrections").fetchone()[0]
        learning = learning_overview(db)
        dictionaries = dict(db.execute("SELECT dictionary_type, COUNT(*) FROM dictionary_entries WHERE enabled=1 GROUP BY dictionary_type"))
    return {"events": events[0], "last_event_at": events[1], "active_rules": learning["active_rules"],
            "corrections": corrections, "total_rules": total_rules, "learning": learning,
            "dictionary_entries": sum(dictionaries.values()), "dictionaries": dictionaries}
