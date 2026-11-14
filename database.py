from config import DB_PATH
from services.sqlite_connection import get_db

from pathlib import Path


DICTIONARY_FILES = {
    "callsigns": "callsigns.txt",
    "commands": "commands.txt",
    "telegram": "telegram.txt",
}




def init_db(db_path=DB_PATH):
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


def import_dictionaries(source_dir=".", db_path=DB_PATH, *, delete_sources=False):
    """
    Однократно импортирует исходные строки TXT-файлов.
    Если категория уже заполнена, импорт остановится,
    чтобы случайно не перезаписать данные.
    """
    source_dir = Path(source_dir)

    files = {}
    original_bytes = {}
    for dictionary_type, filename in DICTIONARY_FILES.items():
        path = source_dir / filename

        if not path.is_file():
            raise FileNotFoundError(f"Файл не знайдено: {path}")

        original_bytes[filename] = path.read_bytes()
        files[dictionary_type] = original_bytes[filename].decode("utf-8-sig").splitlines()

    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
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
                    f"Словник '{dictionary_type}' уже містить записи. "
                    "Імпорт скасовано."
                )

        for dictionary_type, lines in files.items():
            db.executemany(
                """
                INSERT INTO dictionary_entries (
                    dictionary_type, line_number, raw_line, enabled
                )
                VALUES (?, ?, ?, ?)
                """,
                [
                    (dictionary_type, number,
                     line.removeprefix("# @radio345-disabled: "),
                     0 if line.startswith("# @radio345-disabled: ") else 1)
                    for number, line in enumerate(lines, start=1)
                ],
            )

    # Only after all three dictionaries committed. Never delete templates or
    # an operator's file that changed while the transaction was running.
    remaining = []
    if delete_sources:
        for filename, content in original_bytes.items():
            path = source_dir / filename
            try:
                if path.is_symlink() or path.read_bytes() != content:
                    remaining.append(filename)
                else:
                    path.unlink()
            except OSError:
                remaining.append(filename)
    return {"imported": True, "cleanup_remaining": remaining}


def get_dictionary_lines(dictionary_type, db_path=DB_PATH):
    """Возвращает включённые строки в исходном порядке."""
    if dictionary_type not in DICTIONARY_FILES:
        raise ValueError(f"Невідомий словник: {dictionary_type}")

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
