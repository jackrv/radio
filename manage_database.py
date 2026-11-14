#!/usr/bin/env python3
"""Ініціалізація, експорт і явне очищення локальної бази Radio 345."""

import argparse
from contextlib import closing
import socket
import json
from pathlib import Path
import sqlite3

import config
from services.database_lifecycle import (
    initialize_database, export_dictionaries, reset_database, restore_database,
)


def api_running(port=8765):
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "export", "reset", "restore"))
    parser.add_argument("--db", type=Path, default=Path(config.DB_PATH))
    parser.add_argument("--file", type=Path, default=config.DICTIONARIES_PATH,
                        help="Єдиний файл словників (типово dictionaries.txt у корені)")
    parser.add_argument("--backup-dir", type=Path, default=config.BASE_DIR / ".local-backups")
    parser.add_argument("--delete-source", action="store_true", help="Видалити файл словників після успішного імпорту")
    parser.add_argument("--confirm-reset", action="store_true", help="Явно дозволити очищення даних із попередньою резервною копією")
    parser.add_argument("--from", dest="backup", type=Path,
                        help="restore: файл резервної копії з .local-backups/")
    parser.add_argument("--remove-admins", action="store_true", help="Також видалити адміністраторів під час reset")
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize_database(args.db, args.file, delete_source=args.delete_source)
        elif args.action == "restore":
            if args.backup is None:
                parser.error("Для відновлення вкажіть --from .local-backups/<файл>.db")
            if api_running():
                raise RuntimeError("Спочатку зупиніть start_radio345.py або API (порт 8765 зайнятий)")
            result = restore_database(args.db, args.backup, args.backup_dir)
        elif args.action == "export":
            result = {"exported": export_dictionaries(args.db, args.file)}
        else:
            if not args.confirm_reset:
                parser.error("Для очищення потрібен --confirm-reset. Спочатку зупиніть усі служби.")
            result = reset_database(args.db, args.file, args.backup_dir, keep_admins=not args.remove_admins)
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
