"""Один важкий лабораторний процес; не дозволяє конкурувати з моделлю вузла Whisper."""
import os
from contextlib import contextmanager
from pathlib import Path

from shared.instance_lock import acquire_instance_lock
from shared.paths import place, root_of


@contextmanager
def lab_model_slot(db_path):
    root = root_of(db_path)
    lab = acquire_instance_lock(place(root, ".asr-lab.lock"))
    if lab is None:
        raise RuntimeError('Інший лабораторний прогін уже використовує модель')
    receiver = None
    try:
        receiver = acquire_instance_lock(place(root, ".radio345.lock"))
        if receiver is None:
            raise RuntimeError('Вузол Whisper ще тримає модель: ./r345 pause-model')
        yield
    finally:
        if receiver is not None:
            os.close(receiver)
        os.close(lab)


def receiver_model_available(root):
    """Лабораторія не тримає модель."""
    fd = acquire_instance_lock(place(Path(root), ".asr-lab.lock"))
    if fd is None:
        return False
    os.close(fd)
    return True
