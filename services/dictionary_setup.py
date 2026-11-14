"""Перевірка словників перед запуском і імпорт із dictionaries.txt за згодою оператора.

Модуль використовує лише стандартну бібліотеку: лаунчер може працювати
на системному Python. Сам імпорт виконує ``manage_database.py init``
інтерпретатором проєкту, тож правила імпорту залишаються в одному місці.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

from database import DICTIONARY_TYPES, parse_dictionary_file

YES_ANSWERS = {"y", "yes"}


def dictionary_status(db_path) -> dict | None:
    """Кількість рядків словників: ``{тип: (усього, увімкнено)}``.

    ``None`` — бази або таблиці ще немає. Підключення лише для читання,
    тому перевірка ніколи не створює порожню базу.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15.0)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dictionary_entries'"
        ).fetchone()
        if not exists:
            return None
        rows = db.execute(
            "SELECT dictionary_type, COUNT(*), COALESCE(SUM(enabled), 0) "
            "FROM dictionary_entries GROUP BY dictionary_type"
        ).fetchall()
    status = {kind: (0, 0) for kind in DICTIONARY_TYPES}
    for kind, total, enabled in rows:
        if kind in status:
            status[kind] = (int(total), int(enabled))
    return status


def ensure_dictionaries(
    db_path,
    source_path,
    python: str,
    *,
    interactive: bool,
    ask=input,
    out=print,
    run=subprocess.run,
) -> bool:
    """Повертає True, якщо словник позивних готовий до запуску.

    Якщо всі словники порожні, а файл словників є, пропонує імпорт. Без
    інтерактивної консолі лише пояснює, що зробити: імпорт не
    виконується без явного «так» оператора.
    """
    source_path = Path(source_path)
    try:
        status = dictionary_status(db_path)
    except sqlite3.Error:
        out("Помилка: не вдалося прочитати events.db; перевірте файл бази")
        return False

    if status is not None and status["callsigns"][1] > 0:
        return True

    if status is not None and any(total for total, _ in status.values()):
        # import_dictionaries відмовляє, якщо будь-яка категорія вже має рядки.
        out("Помилка: у базі немає увімкнених позивних, але інші словники вже заповнені.")
        out("  Автоматичний імпорт файлу словників неможливий: додайте позивні в Mini App")
        out("  або виконайте manage_database.py reset --confirm-reset і повторіть init.")
        return False

    command_hint = f"{python} manage_database.py init"
    db_exists = Path(db_path).is_file()
    out("⚠️ Словники в events.db порожні." if db_exists
        else "⚠️ Робочої events.db немає — її буде створено під час імпорту.")

    if not source_path.is_file():
        out(f"  Не знайдено {source_path.name}. Скопіюйте dictionaries.example.txt у")
        out(f"  {source_path.name}, заповніть розділи (або відновіть експорт), потім:")
        out(f"  {command_hint}")
        return False

    try:
        sections = parse_dictionary_file(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        out(f"Помилка: не вдалося прочитати {source_path.name} (потрібне кодування UTF-8)")
        return False
    except ValueError as exc:
        out(f"Помилка у {source_path.name}: {exc}")
        return False
    counts = {kind: sum(1 for _, on in rows if on) for kind, rows in sections.items()}
    out(f"  Знайдено в {source_path.name}: " + ", ".join(f"[{kind}] — {n} зап." for kind, n in counts.items()))
    if counts["callsigns"] == 0:
        out("  Розділ [callsigns] не містить жодного позивного; заповніть його й повторіть запуск.")
        return False

    if not interactive:
        out(f"  Консоль неінтерактивна. Виконайте імпорт вручну: {command_hint}")
        return False

    try:
        answer = ask(f"  Імпортувати словники з {source_path.name} у базу зараз? [Y/N]: ")
    except (EOFError, KeyboardInterrupt):
        out("")
        answer = ""
    if answer.strip().lower() not in YES_ANSWERS:
        out("  Імпорт скасовано. Запуск зупинено.")
        return False

    root = Path(__file__).resolve().parent.parent
    # Після імпорту словники живуть у базі; файл потрібен лише для порожнього проєкту.
    result = run([python, str(root / "manage_database.py"), "init", "--delete-source",
                  "--db", str(db_path), "--file", str(source_path)], cwd=root)
    if result.returncode != 0:
        out("Помилка: імпорт не виконано (подробиці вище). Запуск зупинено.")
        return False

    try:
        status = dictionary_status(db_path)
    except sqlite3.Error:
        status = None
    if status is None or status["callsigns"][1] == 0:
        out("Помилка: після імпорту позивних у базі не виявлено. Запуск зупинено.")
        return False
    if source_path.exists():
        out(f"✅ Словники імпортовано. {source_path.name} змінився під час імпорту, тому його залишено.")
    else:
        out(f"✅ Словники імпортовано, {source_path.name} видалено. Повернути файл: manage_database.py export")
    return True


def has_active_admin(db_path) -> bool:
    """Чи є активний адміністратор Mini App (лише читання)."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15.0)) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM telegram_users WHERE role='admin' AND enabled=1"
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def interactive_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()
