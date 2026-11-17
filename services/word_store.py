"""Хранение обученных словесных исправлений Radio 345."""

from contextlib import nullcontext
from services.sqlite_connection import get_db
from core.word_learning import analyze_word_changes
from core.word_rule_validation import classify_pair

MIN_CONFIRMATIONS = 2

REASONS = {
    "number": "змінено номер — це конкретна правка, не правило",
    "number_structure": "кількість чисел змінилась — слова поруч не зіставлено однозначно",
    "inserted": "додане слово — немає пари для заміни",
    "deleted": "видалене слово — немає пари для заміни",
    "apostrophe": "слова з апострофом правила поки не підтримують",
    "long_rewrite": "довге переписування — слова не зіставлено однозначно",
    "conflict": "одне слово виправлено по-різному в одній фразі",
    "callsign_swap": "заміна одного відомого позивного іншим — це зміна адресата",
    "known_name": "правильне словникове ім’я не замінюється",
    "short_word": "надто коротке слово для загального правила",
    "format": "змінено лише регістр або пунктуацію",
    "with_number": "назву змінено разом із номером — можливо, інший адресат",
    "dissimilar": "слова суттєво різні — можлива зміна змісту",
    "word_count": "кількість слів змінилась — можливо, додано або прибрано слово",
    "leftover": "те саме написання лишилося невиправленим в іншому місці фрази",
    "near_callsign": "слово схоже на позивний — потрібна перевірка",
}


def init_word_db(db_path):
    """Создаёт таблицу без изменения существующих данных."""

    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS word_corrections (
                id INTEGER PRIMARY KEY,
                original TEXT NOT NULL,
                corrected TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 0
                    CHECK(confirmations >= 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending', 'active', 'disabled'
                    )),
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(original, corrected),
                CHECK(original <> corrected)
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_word_corrections_lookup
            ON word_corrections(original, status)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS word_correction_votes (
                source_id TEXT NOT NULL,
                original TEXT NOT NULL,
                corrected TEXT NOT NULL,
                PRIMARY KEY (source_id, original),
                CHECK(length(trim(source_id)) > 0),
                CHECK(original <> corrected)
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_word_votes_rule
            ON word_correction_votes(original, corrected)
        """)

        # Рішення оператора щодо неоднозначних пар (лише підтверджено/відхилено).
        db.execute("""
            CREATE TABLE IF NOT EXISTS word_correction_reviews (
                source_id TEXT NOT NULL,
                original TEXT NOT NULL,
                corrected TEXT NOT NULL,
                state TEXT NOT NULL
                    CHECK(state IN ('confirmed', 'rejected')),
                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_id, original, corrected),
                CHECK(original <> corrected)
            )
        """)

        # Міграція без втрати даних: дозвіл оператора для правил звичайних слів.
        columns = {row[1] for row in db.execute("PRAGMA table_info(word_corrections)")}
        if "approved_at" not in columns:
            db.execute("ALTER TABLE word_corrections ADD COLUMN approved_at TEXT")
        if "approved_by" not in columns:
            db.execute("ALTER TABLE word_corrections ADD COLUMN approved_by INTEGER")



def dictionary_names(db) -> frozenset:
    """Імена з увімкнених словників у тій самій транзакції (порожньо без таблиці)."""
    from core.callsigns import Callsigns
    from core.word_rule_validation import known_name_tuples

    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dictionary_entries'").fetchone():
        return frozenset()
    rows = db.execute("""SELECT dictionary_type, raw_line FROM dictionary_entries
        WHERE enabled = 1 ORDER BY dictionary_type, line_number""").fetchall()
    groups = {}
    for kind, line in rows:
        groups.setdefault(kind, []).append(line)
    return known_name_tuples(Callsigns.from_lines(lines) for lines in groups.values())


def _plan(original_text, corrected_text, names):
    """Автоматичні голоси, пари для перевірки й винятки для однієї правки."""
    analysis = analyze_word_changes(original_text, corrected_text)
    auto, review = {}, {}
    excluded = [(label, reason) for label, reason in analysis.excluded]
    for old, new in analysis.pairs:
        kind, reason = classify_pair(old, new, names)
        if kind in ("callsign", "word"):
            auto[(old, new)] = kind
        elif kind == "review":
            review[(old, new)] = reason
        else:
            excluded.append((f"{old} → {new}", reason))
    for old, new, reason in analysis.review:
        kind, own = classify_pair(old, new, names)
        if kind == "excluded":
            excluded.append((f"{old} → {new}", own))
        else:
            review[(old, new)] = reason if kind != "review" or reason == "with_number" else own
    return analysis, auto, review, excluded


def _decisions(db, source_id):
    return {(o, c): state for o, c, state in db.execute(
        "SELECT original, corrected, state FROM word_correction_reviews WHERE source_id = ?",
        (source_id,))}


def _candidate(db, original, corrected, names):
    row = db.execute("""SELECT confirmations, status, approved_at FROM word_corrections
        WHERE original = ? AND corrected = ?""", (original, corrected)).fetchone()
    confirmations, status, approved = row if row else (0, "pending", None)
    kind, _ = classify_pair(original, corrected, names)
    kind = "callsign" if kind == "callsign" else "word"
    conflict = db.execute("""SELECT 1 FROM word_corrections WHERE original = ? AND corrected <> ?
        AND confirmations > 0 AND status <> 'disabled'""", (original, corrected)).fetchone()
    if status == "disabled":
        state = "disabled"
    elif conflict:
        state = "conflict"
    elif confirmations < MIN_CONFIRMATIONS:
        state = "waiting"
    elif kind == "word" and not approved:
        state = "needs_approval"
    else:
        state = "active"
    return {"original": original, "corrected": corrected, "kind": kind,
            "confirmations": confirmations, "required": MIN_CONFIRMATIONS,
            "approved": bool(approved), "state": state}


def build_learning_report(db, source_id, original_text, corrected_text, *, names=None):
    """Що з правки потрапило в навчання. Лише читання."""
    names = dictionary_names(db) if names is None else names
    analysis, auto, review, excluded = _plan(original_text, corrected_text, names)
    decisions = _decisions(db, source_id) if source_id else {}
    votes = set(auto) | {pair for pair in review if decisions.get(pair) == "confirmed"}
    return {
        "text_changed": analysis.text_changed,
        "format_only": analysis.format_only,
        "candidates": [_candidate(db, o, c, names) for o, c in sorted(votes)],
        "review": [{"original": o, "corrected": c, "reason": r, "message": REASONS.get(r, r),
                    "state": decisions.get((o, c), "pending")} for (o, c), r in sorted(review.items())],
        "excluded": [{"label": label, "reason": r, "message": REASONS.get(r, r)} for label, r in excluded],
    }


def _write_votes(db, source_id, pairs):
    previous = set(db.execute("""
        SELECT original, corrected
        FROM word_correction_votes
        WHERE source_id = ?
    """, (source_id,)).fetchall())
    db.execute("DELETE FROM word_correction_votes WHERE source_id = ?", (source_id,))
    current = set()
    for original, corrected in pairs:
        db.execute("""
            INSERT OR IGNORE INTO word_corrections
                (original, corrected)
            VALUES (?, ?)
        """, (original, corrected))
        db.execute("""
            INSERT INTO word_correction_votes
                (source_id, original, corrected)
            VALUES (?, ?, ?)
        """, (source_id, original, corrected))
        current.add((original, corrected))

    # Пересчитываем только затронутые правила.
    for original, corrected in previous | current:
        db.execute("""
            UPDATE word_corrections
            SET confirmations = (
                SELECT COUNT(*)
                FROM word_correction_votes
                WHERE original = ?
                  AND corrected = ?
            ),
            updated_at = CURRENT_TIMESTAMP
            WHERE original = ?
              AND corrected = ?
        """, (original, corrected, original, corrected))

    # Пересчитываем статусы в той же транзакции.
    refresh_word_rule_statuses(db)
    return current


def _sync_source(db, source_id, original_text, corrected_text):
    names = dictionary_names(db)
    _, auto, review, _ = _plan(original_text, corrected_text, names)
    # Рішення оператора зберігаються лише для пар, які ще є в поточній правці.
    for (o, c) in _decisions(db, source_id):
        if (o, c) not in review:
            db.execute("DELETE FROM word_correction_reviews WHERE source_id=? AND original=? AND corrected=?",
                       (source_id, o, c))
    decisions = _decisions(db, source_id)
    pairs, used = [], set()
    for pair in sorted(auto) + sorted(p for p in review if decisions.get(p) == "confirmed"):
        if pair[0] not in used:   # одне джерело — один голос на слово
            used.add(pair[0])
            pairs.append(pair)
    current = _write_votes(db, source_id, pairs)
    return current, names


def save_word_feedback_report(db_path, source_id, original_text, corrected_text, *, db=None):
    """Зберігає голоси одного джерела атомарно й повертає звіт для оператора.

    Повторне виправлення того самого джерела замінює його голоси.
    Неоднозначні пари голосують лише після підтвердження оператором.
    """
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Потрібен ідентифікатор джерела")
    manager = get_db(db_path) if db is None else nullcontext(db)
    with manager as db:
        _, names = _sync_source(db, source_id, original_text, corrected_text)
        return build_learning_report(db, source_id, original_text, corrected_text, names=names)


def save_word_feedback(
    db_path,
    source_id: str,
    original_text: str,
    corrected_text: str,
    *,
    db=None,
) -> int:
    """Сохраняет подтверждения одной записи атомарно; возвращает число голосов."""
    report = save_word_feedback_report(db_path, source_id, original_text, corrected_text, db=db)
    return len(report["candidates"])


def decide_review(db, source_id, original_text, corrected_text, original, corrected, decision):
    """Рішення оператора щодо неоднозначної пари у вже відкритій транзакції."""
    if decision not in ("confirmed", "rejected", "pending"):
        raise ValueError("Невідоме рішення")
    names = dictionary_names(db)
    _, _, review, _ = _plan(original_text, corrected_text, names)
    if (original, corrected) not in review:
        raise LookupError("Цієї пари вже немає в поточній правці")
    if decision == "pending":
        db.execute("DELETE FROM word_correction_reviews WHERE source_id=? AND original=? AND corrected=?",
                   (source_id, original, corrected))
    else:
        db.execute("""INSERT INTO word_correction_reviews (source_id, original, corrected, state)
            VALUES (?, ?, ?, ?) ON CONFLICT(source_id, original, corrected)
            DO UPDATE SET state = excluded.state, updated_at = CURRENT_TIMESTAMP""",
                   (source_id, original, corrected, decision))
    _sync_source(db, source_id, original_text, corrected_text)
    return build_learning_report(db, source_id, original_text, corrected_text, names=names)


def load_approved_word_rules(db_path, min_confirmations: int = MIN_CONFIRMATIONS):
    """Активні правила з окремим дозволом оператора (для звичайних слів)."""
    allowed = set(load_active_word_rules(db_path, min_confirmations))
    with get_db(db_path) as db:
        approved = db.execute(
            "SELECT original, corrected FROM word_corrections WHERE approved_at IS NOT NULL").fetchall()
    return [rule for rule in approved if rule in allowed]


def load_active_word_rules(
    db_path,
    min_confirmations: int = 2,
) -> list[tuple[str, str]]:
    """Возвращает подтверждённые правила без конфликтов."""

    if (
        type(min_confirmations) is not int
        or min_confirmations < 1
    ):
        raise ValueError("Некоректний поріг підтверджень")

    with get_db(db_path) as db:
        rows = db.execute("""
            SELECT w.original, w.corrected
            FROM word_corrections AS w
            WHERE w.status = 'active'
              AND w.confirmations >= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM word_corrections AS other
                  WHERE other.original = w.original
                    AND other.corrected <> w.corrected
                    AND other.confirmations > 0
                    AND other.status <> 'disabled'
              )
            ORDER BY LENGTH(w.original) DESC, w.original
        """, (min_confirmations,)).fetchall()

    return rows


def refresh_word_rule_statuses(
    db,
    min_confirmations: int = 2,
):
    """Обновляет статусы правил внутри существующей транзакции.

    Правило активно, если подтверждений достаточно и нет
    конфликтующего варианта с ненулевым числом голосов.

    Вручную отключённые правила не изменяются.
    """

    if (
        type(min_confirmations) is not int
        or min_confirmations < 1
    ):
        raise ValueError("Некоректний поріг підтверджень")

    db.execute("""
        UPDATE word_corrections
        SET status = CASE
            WHEN confirmations >= ?
             AND NOT EXISTS (
                SELECT 1
                FROM word_corrections AS other
                WHERE other.original = word_corrections.original
                  AND other.corrected <> word_corrections.corrected
                  AND other.confirmations > 0
                  AND other.status <> 'disabled'
             )
            THEN 'active'
            ELSE 'pending'
        END,
        updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('pending', 'active')
    """, (min_confirmations,))
