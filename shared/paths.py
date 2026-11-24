"""Де лежать робочі дані. Усе — в .local-data/; до команди ``r345 move-data`` діє старе місце в корені проєкту."""
from pathlib import Path

DATA = ".local-data"
# Старе ім'я в корені → ім'я в .local-data.
NAMES = {
    "events.db": "events.db", "radio.log": "radio.log", "diagnostics.json": "diagnostics.json",
    ".local-diagnostics": "diagnostics", ".local-recordings": "recordings", ".local-backups": "backups",
    ".local-experiments": "experiments", ".local-notification-sounds": "notification-sounds",
    ".local-segment-spool": "segment-spool", ".local-selections": "selections", ".local-text-checks": "text-checks",
    ".local-telegram-wishlist": "telegram-wishlist", ".local-training": "training", ".local-node": "node",
    ".local-move": "move", ".local-ios": "ios", ".local-home.json": "home.json", ".local-audio.json": "audio.json",
    ".local-commit": "commit", ".local-code.git": "code.git", ".import-move": "import-move",
    ".radio345-launcher": "run", ".radio345-local-admin": "panel-key", ".radio345.lock": "model.lock",
    ".radio345-owner.lock": "owner.lock", ".radio345-worker.lock": "worker.lock", ".asr-lab.lock": "lab.lock",
}


def place(root, name):
    """Чинний шлях: старе місце — лише доки там щось є, а нового ще немає."""
    root = Path(root)
    new, old = root / DATA / NAMES[name], root / name
    if old.exists() and not new.exists():
        return old
    new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return new


def root_of(db_path):
    """Каталог проєкту за шляхом бази: поруч із ним замки, архів і копії."""
    parent = Path(db_path).resolve().parent
    return parent.parent if parent.name == DATA else parent


# Для скриптів, що йдуть на Pi стандартним python3 без проєкту. Нічого не створює: каталог робить той, хто пише.
REMOTE_PLACE = "NAMES = " + repr(NAMES) + '''
import os as _os
def place(root, name):
    new, old = _os.path.join(root, ".local-data", NAMES[name]), _os.path.join(root, name)
    if _os.path.exists(old) and not _os.path.exists(new):
        return old
    return new
'''
