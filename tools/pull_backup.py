"""Команда бекапу Pi: сам код — shared/pull_backup.py (агент launchd копіює його поза ~/Documents)."""
from shared.pull_backup import main

if __name__ == "__main__":
    raise SystemExit(main())
