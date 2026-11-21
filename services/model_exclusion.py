"""Один важкий лабораторний процес; не дозволяє конкурувати з моделлю приймача."""
import os
import time
from contextlib import contextmanager, closing
from pathlib import Path

from core.instance_lock import acquire_instance_lock
from services.sqlite_connection import read_only


@contextmanager
def lab_model_slot(db_path):
    root = Path(db_path).resolve().parent
    lab = acquire_instance_lock(root / '.asr-lab.lock')
    if lab is None:
        raise RuntimeError('Інший лабораторний прогін уже використовує модель')
    receiver = None
    try:
        receiver = acquire_instance_lock(root / '.radio345.lock')
        if receiver is None:
            # Пауза мікрофона не означає вільну пам'ять. Потрібне явне
            # підтвердження вивантаження в чинному режимі «лише запис».
            with closing(read_only(db_path)) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='receiver_capture'").fetchone()
                row = db.execute('SELECT until,unloaded FROM receiver_capture WHERE id=1').fetchone() if exists else None
            if not row or row[0] <= time.time() or row[1] is None:
                raise RuntimeError('Приймач ще тримає модель. Зупиніть його або дочекайтеся підтвердженого режиму «лише запис».')
        yield
    finally:
        if receiver is not None: os.close(receiver)
        os.close(lab)


def receiver_model_available(root):
    """Лише перевірка на старті: працюючий приймач і далі може вивантажитися для черги."""
    fd = acquire_instance_lock(Path(root) / '.asr-lab.lock')
    if fd is None:
        return False
    os.close(fd)
    return True


def acquire_receiver_slot(root, db_path):
    """Старт приймача й скидання давнього unloaded — під тим самим замком лабораторії."""
    lab=acquire_instance_lock(Path(root)/'.asr-lab.lock')
    if lab is None: raise RuntimeError('Лабораторія ще використовує модель; дочекайтеся завершення тесту')
    receiver=None
    try:
        receiver=acquire_instance_lock(Path(root)/'.radio345.lock')
        if receiver is not None:
            from services.receiver_control import mark_capture_unloaded
            mark_capture_unloaded(db_path,False)
        return receiver
    except BaseException:
        if receiver is not None: os.close(receiver)
        raise
    finally: os.close(lab)
