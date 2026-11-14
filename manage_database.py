#!/usr/bin/env python3
"""Ініціалізація, експорт і явне очищення локальної бази Radio 345."""

import argparse
import json
from pathlib import Path
import sqlite3

import config
from services.database_lifecycle import initialize_database, export_dictionaries, reset_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "export", "reset"))
    parser.add_argument("--db", type=Path, default=Path(config.DB_PATH))
    parser.add_argument("--source-dir", type=Path, default=config.BASE_DIR)
    parser.add_argument("--backup-dir", type=Path, default=config.BASE_DIR / ".local-backups")
    parser.add_argument("--delete-sources", action="store_true", help="Видалити лише три робочі TXT після успішного імпорту")
    parser.add_argument("--confirm-reset", action="store_true", help="Явно дозволити очищення даних із попередньою резервною копією")
    parser.add_argument("--remove-admins", action="store_true", help="Також видалити адміністраторів під час reset")
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize_database(args.db, args.source_dir, delete_sources=args.delete_sources)
        elif args.action == "export":
            result = {"exported": export_dictionaries(args.db, args.source_dir)}
        else:
            if not args.confirm_reset:
                parser.error("Для очищення потрібен --confirm-reset. Спочатку зупиніть усі служби.")
            result = reset_database(args.db, args.source_dir, args.backup_dir, keep_admins=not args.remove_admins)
    except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
        # Do not print SQLite SQL fragments or operator dictionary contents.
        if isinstance(exc, (RuntimeError, ValueError, FileExistsError)):
            print(str(exc))
        else:
            print("Операція не завершена: перевірте доступність файлів та SQLite")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
