from config import DB_PATH, DICTIONARIES_PATH
from services.sqlite_connection import get_db

from pathlib import Path


DICTIONARY_TYPES = ("callsigns", "commands", "telegram")
DISABLED_PREFIX = "# @radio345-disabled: "

_FILE_HEADER = """\
# Словники Radio 345. Повний опис формату й приклади: dictionaries.example.txt
# Розділи: [callsigns] — позивні, [commands] — команди, [telegram] — спостереження.
# Рядок «# @radio345-disabled: ...» — вимкнений запис; решта рядків із # — коментарі.
"""


def parse_dictionary_file(text):
    """Розбирає єдиний файл словників на розділи.

    Повертає ``{тип: [(рядок, увімкнено), ...]}`` для всіх трьох типів.
    Коментарі й порожні рядки не є записами словника і не імпортуються.
    """
    result = {kind: [] for kind in DICTIONARY_TYPES}
    seen = set()
    current = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            kind = stripped[1:-1].strip().lower()
            if kind not in result:
                raise ValueError(
                    f"Рядок {number}: невідомий розділ [{kind}]. "
                    "Дозволено: " + ", ".join(f"[{k}]" for k in DICTIONARY_TYPES)
                )
            if kind in seen:
                raise ValueError(f"Рядок {number}: розділ [{kind}] повторюється")
            seen.add(kind)
            current = kind
            continue
        if stripped.startswith(DISABLED_PREFIX.strip()):
            entry = (stripped.removeprefix(DISABLED_PREFIX.strip()).strip(), False)
        elif not stripped or stripped.startswith("#"):
            continue
        else:
            entry = (stripped, True)
        if current is None:
            raise ValueError(
                f"Рядок {number}: запис поза розділом. Додайте перед ним [callsigns], "
                "[commands] або [telegram]"
            )
        if entry[0]:
            result[current].append(entry)
    return result


def format_dictionary_file(entries):
    """Формує текст єдиного файлу з ``{тип: [(рядок, увімкнено), ...]}``."""
    parts = [_FILE_HEADER]
    for kind in DICTIONARY_TYPES:
        parts.append(f"\n[{kind}]\n")
        for line, enabled in entries.get(kind, ()):
            parts.append((line if enabled else DISABLED_PREFIX + line) + "\n")
    return "".join(parts)


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


def import_dictionaries(source_path=DICTIONARIES_PATH, db_path=DB_PATH, *, delete_source=False):
    """
    Однократно імпортує записи з єдиного файлу словників.
    Якщо будь-який словник у базі вже заповнений, імпорт зупиниться,
    щоб випадково не перезаписати дані.
    """
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Файл не знайдено: {source_path}")

    original_bytes = source_path.read_bytes()
    sections = parse_dictionary_file(original_bytes.decode("utf-8-sig"))

    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        for dictionary_type in DICTIONARY_TYPES:
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

        for dictionary_type, entries in sections.items():
            db.executemany(
                """
                INSERT INTO dictionary_entries (
                    dictionary_type, line_number, raw_line, enabled
                )
                VALUES (?, ?, ?, ?)
                """,
                [
                    (dictionary_type, number, line, 1 if enabled else 0)
                    for number, (line, enabled) in enumerate(entries, start=1)
                ],
            )

    # Only after commit. Never delete a file that changed while the
    # transaction was running or that is a symlink.
    remaining = []
    if delete_source:
        try:
            if source_path.is_symlink() or source_path.read_bytes() != original_bytes:
                remaining.append(source_path.name)
            else:
                source_path.unlink()
        except OSError:
            remaining.append(source_path.name)
    return {"imported": {kind: len(rows) for kind, rows in sections.items()},
            "cleanup_remaining": remaining}


def get_dictionary_lines(dictionary_type, db_path=DB_PATH):
    """Возвращает включённые строки в исходном порядке."""
    if dictionary_type not in DICTIONARY_TYPES:
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
