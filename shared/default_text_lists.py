"""Початкові фільтри тексту з shared/default_text_lists.txt.

Один файл для всього першого заповнення списків: нова база копіює розділи в SQLite,
а код бере звідси значення, що діють до першого читання бази (тести, лабораторія).
"""
import re
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).with_name("default_text_lists.txt")
DISABLED = "#"


@lru_cache(maxsize=None)
def sections():
    """{список: (рядки як у панелі, «#» — вимкнений)}; шапка до першого розділу — коментар."""
    out, current = {}, None
    for raw in PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        header = re.fullmatch(r"\[([a-z_]+)\]", line)
        if header:
            current = out.setdefault(header.group(1), [])
        elif line and current is not None:
            current.append(line)
    return {name: tuple(lines) for name, lines in out.items()}


def lines(list_type):
    return sections().get(list_type, ())


def enabled(list_type):
    return tuple(line for line in lines(list_type) if not line.startswith(DISABLED))


def mapping(list_type):
    """«назва: варіант, варіант» → {назва: [варіанти]} лише з увімкнених рядків."""
    result = {}
    for line in enabled(list_type):
        name, _, variants = line.partition(":")
        result[name.strip()] = [v.strip() for v in variants.split(",") if v.strip()]
    return result
