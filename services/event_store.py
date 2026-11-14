from contextlib import nullcontext
from config import DB_PATH
from services.sqlite_connection import get_db
from services.word_store import init_word_db, save_word_feedback


from rapidfuzz import fuzz, process

from callsigns import Callsigns
from core.analyzer import fmt, split_roles




def init_db(db_path=DB_PATH):
    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS radio_events (
                id INTEGER PRIMARY KEY,
                ts TEXT DEFAULT CURRENT_TIMESTAMP,
                callsigns TEXT,
                commands TEXT,
                recipient TEXT,
                sender TEXT,
                text TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY,
                original TEXT UNIQUE,
                corrected TEXT
            )
        """)

        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(radio_events)")
        }

        for col in ("recipient", "sender", "raw_text"):
            if col not in cols:
                db.execute(
                    f"ALTER TABLE radio_events ADD COLUMN {col} TEXT"
                )

    init_word_db(db_path)


def apply_learned_corrections(
    text: str,
    db_path=DB_PATH,
    fuzzy_score=86,
) -> tuple[str, bool]:
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT original, corrected FROM corrections"
        ).fetchall()

    if not rows:
        return text, False

    from callsigns import normalize, to_items

    def numbers_of(value: str) -> list[str]:
        return [
            num for kind, num in to_items(value)
            if kind == "num"
        ]

    norm_text = normalize(text)

    for original, corrected in rows:
        if normalize(original) == norm_text:
            return corrected, True

    # Нечёткая коррекция не должна изменять числовые группы.
    # Точные пользовательские исправления обрабатываются выше.
    source_numbers = numbers_of(text)

    mapping = {
        normalize(original): corrected
        for original, corrected in rows
        if normalize(original)
        and numbers_of(original) == source_numbers
        and numbers_of(corrected) == source_numbers
    }

    if not mapping:
        return text, False

    best = process.extractOne(
        norm_text,
        list(mapping.keys()),
        scorer=fuzz.ratio,
    )

    if best and best[1] >= fuzzy_score:
        return mapping[best[0]], True

    return text, False


def resolve_event_text(event_id: int, db_path=DB_PATH):
    with get_db(db_path) as db:
        row = db.execute(
            "SELECT COALESCE(raw_text, text) FROM radio_events WHERE id = ?",
            (event_id,),
        ).fetchone()

    return row[0] if row else None


def save_correction(
    original: str,
    corrected: str,
    event_id: int | None = None,
    db_path=DB_PATH,
    *,
    source_id: str | None = None,
    db=None,
):
    """Сохраняет обучение и согласованно обновляет событие."""

    manager = (
        get_db(db_path) if db is None
        else nullcontext(db)
    )

    with manager as db:
        fields = None

        if event_id is not None:
            event = db.execute(
                "SELECT id FROM radio_events WHERE id = ?",
                (event_id,),
            ).fetchone()

            if event is None:
                raise LookupError(
                    f"Событие #{event_id} не найдено"
                )

            # Используем актуальные словари из той же SQLite.
            # Отсутствующая таблица вызовет ошибку,
            # а не молчаливую очистку полей события.
            rows = db.execute("""
                SELECT dictionary_type, raw_line
                FROM dictionary_entries
                WHERE enabled = 1
                  AND dictionary_type IN ('callsigns', 'commands')
                ORDER BY dictionary_type, line_number
            """).fetchall()

            lines = {
                "callsigns": [],
                "commands": [],
            }

            for kind, raw_line in rows:
                lines[kind].append(raw_line)

            callsigns = Callsigns.from_lines(
                lines["callsigns"]
            )

            commands = Callsigns.from_lines(
                lines["commands"]
            )

            to_found, from_found = split_roles(
                corrected, callsigns
            )

            fields = (
                fmt(callsigns.find(corrected)),
                fmt(commands.find(corrected)),
                fmt(to_found),
                fmt(from_found),
            )

        # Обновление события и обучение отдельным словам
        # выполняются в одной транзакции.
        # Архивную таблицу corrections больше не пополняем.

        if event_id is not None:
            cs_str, cmd_str, to_str, from_str = fields

            cursor = db.execute(
                """
                UPDATE radio_events
                SET text = ?,
                    callsigns = ?,
                    commands = ?,
                    recipient = ?,
                    sender = ?
                WHERE id = ?
                """,
                (
                    corrected,
                    cs_str,
                    cmd_str,
                    to_str,
                    from_str,
                    event_id,
                ),
            )

            if cursor.rowcount != 1:
                raise LookupError(
                    f"Не удалось обновить событие #{event_id}"
                )

        # Для событий используем постоянный ID из SQLite,
        # для сообщений без события — идентификатор Telegram.
        feedback_source = (
            f"db:{event_id}"
            if event_id is not None
            else source_id
        )

        if feedback_source is not None:
            save_word_feedback(
                db_path,
                feedback_source,
                original,
                corrected,
                db=db,
            )



def save_event(
    callsigns: str,
    commands: str,
    recipient: str,
    sender: str,
    text: str,
    db_path=DB_PATH,
    raw_text: str | None = None,
) -> int:
    with get_db(db_path) as db:
        cursor = db.execute(
            """
            INSERT INTO radio_events
                (callsigns, commands, recipient, sender, text, raw_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (callsigns, commands, recipient, sender, text, raw_text),
        )
        return cursor.lastrowid
