"""Перенести робочі дані з кореня в .local-data (``./r345 move-data``): усі служби зупинено, база перевірена.

Кожен елемент — одне перейменування на тому самому диску; повтор продовжує з місця зупинки. Нове місце, яке
вже є, не перезаписується.
"""
import os
import socket
import sqlite3
from pathlib import Path

from shared.instance_lock import lock_held
from shared.paths import DATA, NAMES

LOCKS = (".radio345.lock", ".radio345-owner.lock", ".radio345-worker.lock", ".asr-lab.lock")
PORTS = (8765, 8766)


def pending(root):
    """Що ще лежить у корені; archives журналу (radio-1.log…) — разом із radio.log."""
    root = Path(root)
    names = [n for n in NAMES if (root / n).exists()]
    return names + sorted(p.name for p in root.glob("radio-*.log"))


def busy(root, ports=PORTS):
    """Хто ще працює з даними: тексти або []."""
    root, found = Path(root), []
    for name in LOCKS:
        for path in (root / name, root / DATA / NAMES[name]):
            if lock_held(path):
                found.append(f"тримається {path.name}")
    if lock_held(root / ".radio345-launcher" / "lock") or lock_held(root / DATA / "run" / "lock"):
        found.append("працює r345")
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                found.append(f"зайнято порт {port}")
    return found


def settle(path):
    """Злити WAL у базу: переносимо один цілий файл. WAL/SHM прибирає сама SQLite під час закриття."""
    db = sqlite3.connect(path, timeout=5)
    try:
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise RuntimeError(f"{path.name}: базу ще використовують")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError(f"{path.name}: не пройшла перевірку цілісності")
    finally:
        db.close()
    for suffix in ("-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            raise RuntimeError(f"{path.name}: є незакритий {suffix}; хтось іще працює з базою")


def move(root, ports=PORTS):
    """Список перенесеного. RuntimeError — нічого з того, що лишилося, не змінено."""
    root = Path(root)
    working = busy(root, ports)
    if working:
        raise RuntimeError("Спершу зупиніть служби: " + "; ".join(working))
    data = root / DATA
    data.mkdir(mode=0o700, exist_ok=True)
    os.chmod(data, 0o700)
    for path in (root / "events.db", root / ".local-recordings" / "index.sqlite3"):
        if path.exists():
            settle(path)
    targets = {name: data / NAMES.get(name, name) for name in pending(root)}
    for target in targets.values():
        if target.exists():
            raise RuntimeError(f"Уже є {DATA}/{target.name}: не перезаписую, перевірте вручну")
    done = []
    for name, target in targets.items():
        os.rename(root / name, target)
        done.append(name)
    return done
