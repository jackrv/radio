import sqlite3
from contextlib import contextmanager
from pathlib import Path


DICTIONARY_FILES = {
    "callsigns": "callsigns.txt",
    "commands": "commands.txt",
    "telegram": "telegram.txt",
}


@contextmanager
def get_db(db_path="events.db"):
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute("PRAGMA busy_timeout=10000")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path="events.db"):
    """Создаёт новые таблицы, не затрагивая существующие."""
    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS dictionary_entries (
                id INTEGER PRIMARY KEY,
                dictionary_type TEXT NOT NULL
                    CHECK(dictionary_type IN (
                        'callsigns', 'commands', 'telegram'
                    )),
                line_number INTEGER NOT NULL,
                raw_line TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(enabled IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dictionary_type, line_number)
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dictionary_type_enabled
            ON dictionary_entries(dictionary_type, enabled)
        """)


def import_dictionaries(source_dir=".", db_path="events.db"):
    """
    Однократно импортирует исходные строки TXT-файлов.
    Если категория уже заполнена, импорт остановится,
    чтобы случайно не перезаписать данные.
    """
    source_dir = Path(source_dir)

    files = {}
    for dictionary_type, filename in DICTIONARY_FILES.items():
        path = source_dir / filename

        if not path.is_file():
            raise FileNotFoundError(f"Не найден файл: {path}")

        files[dictionary_type] = path.read_text(
            encoding="utf-8-sig"
        ).splitlines()

    with get_db(db_path) as db:
        for dictionary_type in DICTIONARY_FILES:
            exists = db.execute(
                """
                SELECT COUNT(*)
                FROM dictionary_entries
                WHERE dictionary_type = ?
                """,
                (dictionary_type,),
            ).fetchone()[0]

            if exists:
                raise RuntimeError(
                    f"Словарь '{dictionary_type}' уже содержит записи. "
                    "Импорт отменён."
                )

        for dictionary_type, lines in files.items():
            db.executemany(
                """
                INSERT INTO dictionary_entries (
                    dictionary_type, line_number, raw_line
                )
                VALUES (?, ?, ?)
                """,
                [
                    (dictionary_type, number, line)
                    for number, line in enumerate(lines, start=1)
                ],
            )


def get_dictionary_lines(dictionary_type, db_path="events.db"):
    """Возвращает включённые строки в исходном порядке."""
    if dictionary_type not in DICTIONARY_FILES:
        raise ValueError(f"Неизвестный словарь: {dictionary_type}")

    with get_db(db_path) as db:
        rows = db.execute(
            """
            SELECT raw_line
            FROM dictionary_entries
            WHERE dictionary_type = ? AND enabled = 1
            ORDER BY line_number
            """,
            (dictionary_type,),
        ).fetchall()

    return [row[0] for row in rows]
