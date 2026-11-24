"""Навчені заміни слів: черга, стан, вмикання й дозвіл оператора."""
import time
from shared.sqlite_connection import get_db
from shared.user_store import _require_permission
from shared.word_store import refresh_word_rule_statuses
from shared.management.common import ManagementConflict
from shared.management.common import version


_RULE_COLUMNS = "id, original, corrected, confirmations, status, approved_at"


def _rule_item(row, names):
    from shared.word_rule_validation import classify_pair
    item = dict(zip(("id", "original", "corrected", "confirmations", "status", "approved_at"), row))
    kind, reason = classify_pair(item["original"], item["corrected"], names)
    # callsign — лише перед дозволеним номером; word — після дозволу оператора; blocked — ніколи.
    item["kind"] = kind if kind == "callsign" else "word" if kind in ("word", "review") else "blocked"
    item["blocked_reason"] = reason if item["kind"] == "blocked" else None
    item["approved"] = item.pop("approved_at") is not None
    item["applies"] = (item["status"] == "active" and item["kind"] != "blocked"
                       and (item["kind"] == "callsign" or item["approved"]))
    item["needs_approval"] = item["kind"] == "word" and not item["approved"]
    item["version"] = version(row)
    return item


def _rule_state(item, min_confirmations):
    """Чому правило діє або не діє — одним словом для панелі."""
    if item["kind"] == "blocked":
        return "blocked"
    if item["status"] == "disabled":
        return "disabled"
    if item["applies"]:
        return "applies"
    if item["confirmations"] < min_confirmations:
        return "needs_confirmation"
    if item["status"] != "active":
        return "conflict"           # підтверджень досить, але є інший варіант того самого слова
    return "needs_approval"


def learning_queue(db_path):
    """Усі навчені заміни з причиною, чому кожна діє чи ні, і правками-джерелами (лише читання)."""
    from shared.word_store import MIN_CONFIRMATIONS, dictionary_names, init_word_db
    init_word_db(db_path)
    with get_db(db_path) as db:
        names = dictionary_names(db)
        rows = db.execute(f"SELECT {_RULE_COLUMNS}, updated_at FROM word_corrections ORDER BY id DESC").fetchall()
        sources = {}
        for source_id, original, corrected in db.execute(
                "SELECT source_id, original, corrected FROM word_correction_votes ORDER BY rowid"):
            sources.setdefault((original, corrected), []).append(source_id)
    items, counts = [], {}
    for row in rows:
        item = _rule_item(row[:6], names)
        item["updated_at"] = row[6]
        item["state"] = _rule_state(item, MIN_CONFIRMATIONS)
        item["sources"] = sources.get((item["original"], item["corrected"]), [])
        counts[item["state"]] = counts.get(item["state"], 0) + 1
        items.append(item)
    return {"items": items, "counts": counts, "total": len(items), "min_confirmations": MIN_CONFIRMATIONS}


def _locked_rule(db, actor_id, rule_id, expected_version):
    _require_permission(db, actor_id, "dictionaries.write")
    row = db.execute(f"SELECT {_RULE_COLUMNS} FROM word_corrections WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise LookupError("Правило не знайдено")
    if version(row) != expected_version:
        raise ManagementConflict("Правило змінилося. Онови список.")
    return row


def toggle_rule(db_path, actor_id, rule_id, disabled, expected_version):
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _locked_rule(db, actor_id, rule_id, expected_version)
        db.execute("UPDATE word_corrections SET status = ? WHERE id = ?",
                   ("disabled" if disabled else "pending", rule_id))
        # Re-enabling never bypasses confirmation or conflict checks.
        refresh_word_rule_statuses(db)
    return {"saved": True}


def approve_rule(db_path, actor_id, rule_id, approved, expected_version):
    """Дозвіл застосовувати правило звичайного слова. Підтвердження й конфлікти діють як раніше."""
    from shared.word_store import dictionary_names
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = _locked_rule(db, actor_id, rule_id, expected_version)
        if approved and _rule_item(row, dictionary_names(db))["kind"] != "word":
            raise ValueError("Дозвіл потрібен лише для правил звичайних слів")
        db.execute("UPDATE word_corrections SET approved_at = ?, approved_by = ? WHERE id = ?",
                   (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) if approved else None,
                    actor_id if approved else None, rule_id))
    return {"saved": True}


def learning_overview(db):
    """Окремі лічильники навчання — не змішуються між собою."""
    from shared.word_store import dictionary_names
    names = dictionary_names(db)
    rows = db.execute(f"SELECT {_RULE_COLUMNS} FROM word_corrections").fetchall()
    items = [_rule_item(row, names) for row in rows]
    candidates = [i for i in items if i["confirmations"] > 0 and i["status"] != "disabled"]
    return {
        "candidates": len(candidates),
        "confirmations": sum(i["confirmations"] for i in candidates),
        "active_rules": sum(i["applies"] for i in items),
        "awaiting_approval": sum(i["status"] == "active" and i["needs_approval"] for i in items),
        "waiting_confirmations": sum(i["status"] == "pending" for i in candidates),
        "review_decisions": db.execute("SELECT COUNT(*) FROM word_correction_reviews").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='word_correction_reviews'").fetchone() else 0,
    }
