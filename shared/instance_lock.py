"""Один приймач на Mac: блокування файла, яке ОС знімає навіть після аварії."""

import fcntl
import os


OWNER_LOCK = ".radio345-owner.lock"   # хто пише події й веде бота; модель тримає окремий .radio345.lock


def acquire_instance_lock(path):
    """Повертає дескриптор, що тримає блокування до завершення процесу, або None."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def lock_held(path):
    """Не створює, не видаляє і не перезаписує lock-файл."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)
