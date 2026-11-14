"""Хранение обученных словесных исправлений Radio 345."""

from contextlib import nullcontext
from services.sqlite_connection import get_db
from core.word_learning import extract_word_changes


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



def save_word_feedback(
    db_path,
    source_id: str,
    original_text: str,
    corrected_text: str,
    *,
    db=None,
) -> int:
    """Сохраняет подтверждения одной записи атомарно.

    Повторное исправление того же источника заменяет его
    предыдущие голоса, а не увеличивает их количество.
    """

    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Потрібен ідентифікатор джерела")

    changes = extract_word_changes(
        original_text, corrected_text
    )

    # Одно исходное слово не должно подтверждать
    # два разных варианта в рамках одной записи.
    candidates = {}
    conflicts = set()

    for original, corrected in changes:
        if (
            original in candidates
            and candidates[original] != corrected
        ):
            conflicts.add(original)
        else:
            candidates[original] = corrected

    for original in conflicts:
        candidates.pop(original, None)

    manager = get_db(db_path) if db is None else nullcontext(db)

    with manager as db:
        previous = set(db.execute("""
            SELECT original, corrected
            FROM word_correction_votes
            WHERE source_id = ?
        """, (source_id,)).fetchall())

        db.execute("""
            DELETE FROM word_correction_votes
            WHERE source_id = ?
        """, (source_id,))

        current = set()

        for original, corrected in candidates.items():
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
            """, (
                original, corrected,
                original, corrected,
            ))

        # Пересчитываем статусы в той же транзакции.
        refresh_word_rule_statuses(db)

    return len(current)


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
