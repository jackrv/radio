"""Вибір інтерпретатора проєкту без імпорту аудіо або моделей."""

from pathlib import Path
import sys


def select_python(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    python = root / ".venv" / "bin" / "python"
    if python.is_file():
        return str(python.absolute())
    return sys.executable
