"""Приватні локальні файли без переходу за символічними посиланнями."""
import os
import stat
from pathlib import Path


def private_open(path, flags=os.O_RDWR | os.O_CREAT):
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Очікується звичайний приватний файл")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def protect_existing(path):
    # Не відкривати/закривати файл: на POSIX close() іншого дескриптора
    # скидає SQLite-блокування цього inode у всіх потоках процесу.
    try:
        info = Path(path).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Очікується звичайний приватний файл")
        os.chmod(path, 0o600, follow_symlinks=False)
    except FileNotFoundError:
        # SQLite штатно видаляє WAL/SHM при закритті останнього з'єднання,
        # зокрема між lstat та chmod у паралельному процесі.
        return
