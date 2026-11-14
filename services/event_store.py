from contextlib import nullcontext
from config import DB_PATH
from services.sqlite_connection import get_db
from services.word_store import init_word_db, save_word_feedback
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

        cols = {
            row[1]
            for row in db.execute("PRAGMA table_info(radio_events)")
        }

        for col in ("recipient", "sender", "raw_text"):
            if col not in cols:
                db.execute(
                    f"ALTER TABLE radio_events ADD COLUMN {col} TEXT"
                )

        # Keep IDs monotonic even after an intentional data reset: old Telegram
        # db:<id> buttons must never address a newly recognized event.
        db.execute("""CREATE TABLE IF NOT EXISTS radio_event_sequence (
            id INTEGER PRIMARY KEY CHECK(id=1), next_id INTEGER NOT NULL
        )""")
        db.execute("""INSERT OR IGNORE INTO radio_event_sequence (id,next_id)
            SELECT 1, COALESCE(MAX(id),0)+1 FROM radio_events""")

    init_word_db(db_path)


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
                    f"Подія #{event_id} не знайдено"
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
                    f"Не вдалося оновити подію #{event_id}"
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
        db.execute("""UPDATE radio_event_sequence SET next_id=MAX(next_id,
            (SELECT COALESCE(MAX(id),0)+1 FROM radio_events))+1 WHERE id=1""")
        event_id = db.execute("SELECT next_id-1 FROM radio_event_sequence WHERE id=1").fetchone()[0]
        cursor = db.execute(
            """
            INSERT INTO radio_events
                (id, callsigns, commands, recipient, sender, text, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, callsigns, commands, recipient, sender, text, raw_text),
        )
        return cursor.lastrowid
