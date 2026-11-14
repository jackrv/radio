"""Один приймач на Mac: блокування файла, яке ОС знімає навіть після аварії."""

import fcntl
import os


def acquire_instance_lock(path):
    """Повертає дескриптор, що тримає блокування до завершення процесу, або None."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd
