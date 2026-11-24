"""Стан пристроїв для панелі: цей Mac (r345) і Pi через SSH. Лише читання, без секретів і текстів ефіру.

Pi — RADIO345_PI_SSH у .env (напр. користувач@radio345); ключ має працювати без пароля. З Pi на Mac не ходимо.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = ("node", "asr", "panel", "api", "tunnel")
HOME_PROCESSES = ("asr",)      # хто веде базу й бота — той «дім»
FRESH_S = 10.0
SSH_TIMEOUT_S = 12

# Виконується стандартним python3 на Pi зі stdin; виводить один рядок JSON.
REMOTE = r'''
import hashlib, json, os, shutil, sqlite3, subprocess, sys, time
root = os.path.expanduser("~/" + sys.argv[1])
out = {"processes": {}}
for name in sys.argv[2].split(","):
    unit = "radio345-" + name
    r = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState,UnitFileState,NRestarts,ExecMainStatus"], capture_output=True, text=True).stdout
    p = dict(l.split("=", 1) for l in r.splitlines() if "=" in l)
    out["processes"][name] = {"alive": p.get("ActiveState") == "active", "enabled": p.get("UnitFileState") == "enabled",
                              "restarts": int(p.get("NRestarts") or 0),
                              "blocked": p.get("ActiveState") != "active" and p.get("ExecMainStatus") == "75"}
try:
    d = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, "events.db"), uri=True, timeout=3)
    out["events"], out["last_event"] = d.execute("SELECT COUNT(*), MAX(CAST(strftime('%s', ts) AS INTEGER)) FROM radio_events").fetchone()
    d.close()
except sqlite3.Error:
    out["events"] = None
try:
    out["queue"] = sum(n.endswith(".seg") for n in os.listdir(os.path.join(root, ".local-segment-spool")))
except OSError:
    out["queue"] = 0
digest = hashlib.sha256()
try:
    with open(os.path.join(root, "deploy/profiles/pi.files")) as f:
        names = [l.strip().lstrip("/") for l in f if l.strip() and not l.startswith("#")]
    for name in names:
        digest.update(name.encode() + b"\0")
        try:
            with open(os.path.join(root, name), "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
        except OSError:
            digest.update(b"missing")
    out["code"] = digest.hexdigest()[:12]
except OSError:
    out["code"] = None
out["disk_free"] = shutil.disk_usage(root).free
try:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        out["temp_c"] = round(int(f.read()) / 1000, 1)
except (OSError, ValueError):
    out["temp_c"] = None
out["uptime_s"] = time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else None
print(json.dumps(out))
'''


def code_hash(root=ROOT):
    """Той самий підсумок, що REMOTE рахує на Pi: файли, які їй призначено."""
    digest = hashlib.sha256()
    names = (root / "deploy/profiles/pi.files").read_text(encoding="utf-8").splitlines()
    for name in (n.strip().lstrip("/") for n in names if n.strip() and not n.startswith("#")):
        digest.update(name.encode() + b"\0")
        try:
            digest.update(hashlib.sha256((root / name).read_bytes()).digest())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:12]


def local(db_path, root=ROOT):
    from shared.event_store import get_db
    from shared.model_pause import ModelPause, model_root
    from shared.r345_control import Control
    running = Control(root / ".radio345-launcher").read()
    processes = {n: {"alive": bool(p.get("alive")), "enabled": True, "restarts": p.get("restarts", 0),
                     "blocked": bool(p.get("blocked"))}
                 for n, p in (running or {}).get("processes", {}).items()}
    with get_db(db_path) as db:
        events, last = db.execute("SELECT COUNT(*), MAX(CAST(strftime('%s', ts) AS INTEGER)) FROM radio_events").fetchone()
    spool = Path(os.environ.get("RADIO_SPOOL_DIR", "").strip() or root / ".local-segment-spool")
    return {"reachable": True, "profile": (running or {}).get("profile"), "processes": processes,
            "model": ModelPause(model_root(root)).state(), "events": events, "last_event": last,
            "queue": sum(1 for _ in spool.glob("*.seg")) if spool.is_dir() else 0,
            "code": code_hash(root), "disk_free": shutil.disk_usage(root).free}


def pi_host(env=None):
    host = (os.environ if env is None else env).get("RADIO345_PI_SSH", "").strip()
    return host if host and not host.startswith("-") and re.fullmatch(r"[\w.@-]+", host) else None


def remote(host, folder="radio345-hub", run=subprocess.run):
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host,
               f"python3 -I - {folder} {','.join(UNITS)}"]
    try:
        done = run(command, input=REMOTE, capture_output=True, text=True, timeout=SSH_TIMEOUT_S)
        if done.returncode:
            return {"reachable": False, "error": "SSH недоступний" if done.returncode == 255 else "Pi повернула помилку"}
        data = json.loads(done.stdout.strip().splitlines()[-1])
        if not isinstance(data, dict) or not isinstance(data.get("processes"), dict):
            raise ValueError
    except subprocess.TimeoutExpired:
        return {"reachable": False, "error": "Pi не відповіла вчасно"}
    except (OSError, ValueError, IndexError):
        return {"reachable": False, "error": "Незрозуміла відповідь Pi"}
    return {"reachable": True, **data}


def home_of(devices):
    """Де працює бот із базою: "mac", "pi", "both" (конфлікт), None — ніде; "unknown" — Pi не видно."""
    def runs(device):
        return any((device.get("processes") or {}).get(n, {}).get("alive") for n in HOME_PROCESSES)
    mac, pi = runs(devices["mac"]), devices.get("pi")
    if pi is not None and not pi.get("reachable"):
        return "mac" if mac else "unknown"
    both = mac and pi is not None and runs(pi)
    return "both" if both else "mac" if mac else "pi" if pi is not None and runs(pi) else None


class Devices:
    """Кеш на FRESH_S: відкрита вкладка не має постійно ходити на Pi."""

    def __init__(self, db_path, root=ROOT, host=None, fetch=remote, clock=time.monotonic):
        self.db_path, self.root, self.fetch, self.clock = db_path, root, fetch, clock
        self.host = host
        self.lock, self._at, self._data = threading.Lock(), float("-inf"), None

    def read(self):
        with self.lock:
            if self._data is None or self.clock() - self._at >= FRESH_S:
                mac = local(self.db_path, self.root)
                pi = self.fetch(self.host) if self.host else None
                devices = {"mac": mac, "pi": pi}
                current = pi.get("code") if pi else None
                self._data = {**devices, "configured": bool(self.host), "home": home_of(devices),
                              "code_matches": None if current is None else current == mac["code"], "at": time.time()}
                self._at = self.clock()
            return self._data
