"""Одна зміна пристроїв за раз — перенесення чи оновлення, з панелі або з консолі (flock, знімається сам)."""
import os
from contextlib import contextmanager
from pathlib import Path

from shared.instance_lock import acquire_instance_lock
from shared.paths import place


class Busy(Exception):
    pass


@contextmanager
def exclusive(root):
    folder = place(Path(root), ".local-move")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = acquire_instance_lock(folder / "busy.lock")
    if fd is None:
        raise Busy("Вже триває перенесення чи оновлення")
    try:
        yield
    finally:
        os.close(fd)
