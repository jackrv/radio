"""Де «дім» (база, бот, Mini App): файл .local-home.json на кожному пристрої.

Немає файла — обмежень немає. Вказано інший пристрій — розпізнавання з ботом і API тут не запускаються,
а панель не змінює дані.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

NAME = ".local-home.json"
DEVICES = ("mac", "pi")


def this_device():
    return "mac" if sys.platform == "darwin" else "pi"


def read(root):
    try:
        data = json.loads((Path(root) / NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("home") in DEVICES and type(data.get("epoch")) is int:
        return {"home": data["home"], "epoch": data["epoch"]}
    return None


def write(root, home, epoch):
    if home not in DEVICES:
        raise ValueError("Невідомий пристрій")
    fd, temp = tempfile.mkstemp(dir=root)   # 0600
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"home": home, "epoch": int(epoch)}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, Path(root) / NAME)


def foreign(root, device=None):
    """Дім — на іншому пристрої: його назва або None."""
    marker = read(root)
    return marker["home"] if marker and marker["home"] != (device or this_device()) else None
