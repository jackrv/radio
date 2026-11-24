"""Стан пристроїв для панелі: цей Mac (r345) і Pi через SSH. Без секретів і текстів ефіру.

Pi — RADIO345_PI_SSH у .env (напр. користувач@radio345); ключ має працювати без пароля. З Pi на Mac не ходимо.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from shared import home_marker
from shared.paths import REMOTE_PLACE, place

ROOT = Path(__file__).resolve().parent.parent
UNITS = ("node", "asr", "panel", "api", "tunnel")
HOME_PROCESSES = ("asr",)      # хто веде базу й бота — той «дім»
FRESH_S = 10.0
SSH_TIMEOUT_S = 12

# Виконується стандартним python3 на Pi зі stdin; виводить один рядок JSON.
REMOTE = REMOTE_PLACE + r'''
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
    d = sqlite3.connect("file:%s?mode=ro" % place(root, "events.db"), uri=True, timeout=3)
    out["events"], out["last_event"] = d.execute("SELECT COUNT(*), MAX(CAST(strftime('%s', ts) AS INTEGER)) FROM radio_events").fetchone()
    d.close()
except sqlite3.Error:
    out["events"] = None
try:
    out["queue"] = sum(n.endswith(".seg") for n in os.listdir(place(root, ".local-segment-spool")))
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
try:
    with open("/proc/asound/pcm") as f:
        out["audio"] = any("capture" in l for l in f)
except OSError:
    out["audio"] = False
try:
    with open(place(root, ".local-commit")) as f:
        out["commit"] = f.read(40)
except OSError:
    out["commit"] = None
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


def commit(root=ROOT):
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def local(db_path, root=ROOT):
    from shared.event_store import get_db
    from shared.model_pause import ModelPause, model_root
    from shared.r345_control import Control
    running = Control(place(root, ".radio345-launcher")).read()
    processes = {n: {"alive": bool(p.get("alive")), "enabled": p.get("wanted", True), "restarts": p.get("restarts", 0),
                     "blocked": bool(p.get("blocked"))}
                 for n, p in (running or {}).get("processes", {}).items()}
    with get_db(db_path) as db:
        events, last = db.execute("SELECT COUNT(*), MAX(CAST(strftime('%s', ts) AS INTEGER)) FROM radio_events").fetchone()
    spool = Path(os.environ.get("RADIO_SPOOL_DIR", "").strip() or place(root, ".local-segment-spool"))
    return {"reachable": True, "profile": (running or {}).get("profile"), "processes": processes,
            "model": ModelPause(model_root(root)).state(), "events": events, "last_event": last,
            "queue": sum(1 for _ in spool.glob("*.seg")) if spool.is_dir() else 0,
            "code": code_hash(root), "commit": commit(root), "disk_free": shutil.disk_usage(root).free}


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


# Дія → аргументи systemctl. enable/disable --now: стан переживає перезавантаження Pi. Список дозволено в deploy/hub/control.sh.
SYSTEMCTL = {"start": "enable --now", "stop": "disable --now", "restart": "restart"}
PI_CONTROLLED = ("node", "asr", "panel", "api", "tunnel")
MAC_CONTROLLED = ("node", "asr", "api", "tunnel", "worker")   # панель не зупиняє сама себе


class Refused(Exception):
    """Дію відхилено; текст показується оператору."""


def control_pi(host, process, verb, run=subprocess.run):
    if process not in PI_CONTROLLED or verb not in SYSTEMCTL:
        raise Refused("Невідома дія")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host,
               f"sudo -n /usr/bin/systemctl {SYSTEMCTL[verb]} radio345-{process}"]
    try:
        done = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("Pi не відповіла вчасно; перевірте стан") from None
    if done.returncode == 255:
        raise Refused("SSH до Pi недоступний")
    if done.returncode:
        raise Refused("Немає дозволу: на Pi один раз sudo sh deploy/hub/control.sh" if "password" in (done.stderr or "").lower()
                      else "Pi не виконала дію; перевірте стан")


def control_mac(process, verb, root=ROOT):
    from shared.r345_control import VERBS, Control
    if process not in MAC_CONTROLLED or verb not in VERBS:
        raise Refused("Невідома дія")
    control = Control(place(root, ".radio345-launcher"))
    running = control.read()
    if running is None:
        raise Refused("Службами на Mac керує не r345")
    if process not in running["processes"]:
        raise Refused(f"Профіль {running.get('profile')} не має цього процесу")
    control.request(verb, [process])


def audio_pi(host, *args, folder="radio345-hub", run=subprocess.run):
    """shared.audio_choice на Pi; аргументи екрановано."""
    import shlex
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host,
               f"cd {folder} && .venv/bin/python -m shared.audio_choice " + " ".join(shlex.quote(a) for a in args)]
    try:
        done = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        if done.returncode:
            raise Refused("SSH до Pi недоступний" if done.returncode == 255 else "Pi не підтримує вибір звуку: оновіть її код")
        data = json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("Pi не відповіла вчасно") from None
    except (ValueError, IndexError):
        raise Refused("Незрозуміла відповідь Pi") from None
    return data


def home_of(devices):
    """Де працює бот із базою: "mac", "pi", "both" (конфлікт), None — ніде; "unknown" — Pi не видно."""
    def runs(device):
        return any((device.get("processes") or {}).get(n, {}).get("alive") for n in HOME_PROCESSES)
    mac, pi = runs(devices["mac"]), devices.get("pi")
    if pi is not None and not pi.get("reachable"):
        return "mac" if mac else "unknown"
    both = mac and pi is not None and runs(pi)
    return "both" if both else "mac" if mac else "pi" if pi is not None and runs(pi) else None


# Режим роботи → які служби потрібні. Тунель (публічна адреса) вмикають окремо, панель не чіпаємо.
MODES = {"pi": {"mac": ("worker",), "pi": ("api", "asr", "node")},
         "mac": {"mac": ("worker", "api", "asr", "node"), "pi": ()},
         "off": {"mac": (), "pi": ()}}
MODE_STOP = ("node", "asr", "tunnel", "api", "worker")   # ефір — першим, модель — останньою


def mode_of(devices, marker):
    """Поточний режим: "off" — ефір, бот і API ніде не працюють; інакше дім за позначкою, без неї — де бот."""
    def core(device):
        return any((device.get("processes") or {}).get(n, {}).get("alive") for n in ("node", "asr", "api"))
    pi = devices.get("pi")
    home = marker["home"] if marker else home_of(devices)
    if not core(devices["mac"]) and (pi is None or pi.get("reachable") and not core(pi)):
        return "off"
    return home if home in ("mac", "pi") else None


class ThisPi:
    """Панель на самій Pi: лише її служби й звук. Права — вузьке правило polkit (deploy/hub/control.sh), не sudo."""

    def __init__(self, root, run=subprocess.run, clock=time.monotonic):
        self.root, self.run, self.clock = Path(root), run, clock
        self.lock, self._at, self._data = threading.Lock(), float("-inf"), None

    def _state(self):
        folder = os.path.relpath(self.root, Path.home())
        try:
            done = self.run([sys.executable, "-I", "-", folder, ",".join(UNITS)], input=REMOTE, capture_output=True, text=True,
                            timeout=SSH_TIMEOUT_S)
            return {"reachable": True, **json.loads(done.stdout.strip().splitlines()[-1])}
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            return {"reachable": False, "error": "Не вдалося прочитати стан служб"}

    def read(self):
        with self.lock:
            if self._data is None or self.clock() - self._at >= FRESH_S:
                found = {"mac": {"processes": {}}, "pi": self._state()}
                marker = home_marker.read(self.root)
                mode = mode_of(found, marker)
                self._data = {"mac": None, "pi": found["pi"], "self": "pi", "configured": False, "home": home_of(found),
                              "marker": marker and marker["home"], "mode": mode, "needed": MODES.get(mode),
                              "code_matches": None, "at": time.time()}
                self._at = self.clock()
            return self._data

    def act(self, device, process, verb):
        if device != "pi" or process not in PI_CONTROLLED or verb not in SYSTEMCTL or (process, verb) == ("panel", "stop"):
            raise Refused("Ця панель керує лише службами Pi")
        command = ["systemctl", "--no-ask-password", verb, f"radio345-{process}"]
        if process == "panel":
            command.insert(1, "--no-block")   # перезапускаємо себе
        try:
            done = self.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused("Служба не відповіла вчасно; перевірте стан") from None
        finally:
            with self.lock:
                self._at = float("-inf")
        if done.returncode:
            denied = any(w in (done.stderr or "").lower() for w in ("authentication", "access denied", "not authorized", "permission"))
            raise Refused("Немає дозволу: на Pi один раз sudo sh deploy/hub/control.sh" if denied else "Дію не виконано; перевірте стан")

    def audio(self, device):
        from shared import audio_choice
        if device != "pi":
            raise Refused("Ця панель керує лише звуком Pi")
        return audio_choice.listing(self.root)

    def save_audio(self, device, values, revision):
        from shared import audio_choice
        if device != "pi":
            raise Refused("Ця панель керує лише звуком Pi")
        return audio_choice.write(self.root, values, revision)


class Devices:
    """Кеш на FRESH_S: відкрита вкладка не має постійно ходити на Pi."""

    def __init__(self, db_path, root=ROOT, host=None, fetch=remote, clock=time.monotonic):
        self.db_path, self.root, self.fetch, self.clock = db_path, root, fetch, clock
        self.host = host
        self.lock, self._at, self._data = threading.Lock(), float("-inf"), None

    def act(self, device, process, verb, control_pi=control_pi, control_mac=control_mac):
        """Одна служба на одному пристрої. Другого бота чи тунелю не запускає."""
        if device == "pi" and not self.host:
            raise Refused("Не налаштовано RADIO345_PI_SSH")
        if verb in ("start", "restart") and process in ("asr", "tunnel"):
            with self.lock:
                self._at = float("-inf")
            state = self.read()
            other = state["pi" if device == "mac" else "mac"]
            if other is not None and not other.get("reachable"):
                pass   # другого не видно: перевірить сама служба (shared/conflict.py)
            elif other is not None and (other.get("processes") or {}).get(process, {}).get("alive"):
                raise Refused("Уже працює на іншому пристрої: спершу зупиніть там")
        if device == "pi":
            control_pi(self.host, process, verb)
        else:
            control_mac(process, verb, self.root)
        with self.lock:
            self._at = float("-inf")

    def apply_mode(self, mode, control_pi=control_pi, control_mac=control_mac):
        """Увімкнути потрібні служби режиму й зупинити зайві. Дім не переносить. Повертає зауваження або None."""
        with self.lock:
            self._at = float("-inf")
        state = self.read()
        mac, pi = state["mac"], state["pi"]
        if mode != "off":
            home = state["marker"] or state["home"] or ("mac" if pi is None else None)
            if home != mode:
                raise Refused("Дім на іншому пристрої: спершу перенесіть його" if home else "Не видно, де дім: увімкніть бота на потрібному пристрої")
        seen = pi is not None and pi.get("reachable")
        if mode == "pi" and not seen:
            raise Refused("Pi недоступна")

        def control(device, process, verb):
            if device == "pi":
                control_pi(self.host, process, verb)
            else:
                control_mac(process, verb, self.root)
        sides = {"mac": mac.get("processes") or {}, **({"pi": pi["processes"]} if seen else {})}
        try:
            for device, processes in sides.items():   # спершу зупинити зайве: двох ботів не буде
                for process in MODE_STOP:
                    item = processes.get(process)
                    if item and process not in MODES[mode][device] and not (process == "tunnel" and device == mode):
                        if item.get("alive") or item.get("enabled"):
                            control(device, process, "stop")
            for device in sides:
                for process in MODES[mode][device]:
                    if not sides[device].get(process, {}).get("alive"):
                        control(device, process, "start")
        finally:
            with self.lock:
                self._at = float("-inf")
        return "Pi недоступна: її служби не змінено" if pi is not None and not seen else None

    def audio(self, device):
        from shared import audio_choice
        if device == "mac":
            return audio_choice.listing(self.root)
        if not self.host:
            raise Refused("Не налаштовано RADIO345_PI_SSH")
        return audio_pi(self.host, "read")

    def save_audio(self, device, values, revision):
        from shared import audio_choice
        if device == "mac":
            return audio_choice.write(self.root, values, revision)
        if not self.host:
            raise Refused("Не налаштовано RADIO345_PI_SSH")
        values = {k: audio_choice.clean_name(values.get(k, "")) for k in audio_choice.KEYS}
        data = audio_pi(self.host, "write", json.dumps({"values": values, "revision": revision}, ensure_ascii=False))
        if "conflict" in data:
            raise audio_choice.Conflict(data["conflict"])
        if "settings" not in data:
            raise Refused("Pi відхилила налаштування звуку")
        return data["settings"]

    def read(self):
        with self.lock:
            if self._data is None or self.clock() - self._at >= FRESH_S:
                mac = local(self.db_path, self.root)
                pi = self.fetch(self.host) if self.host else None
                devices = {"mac": mac, "pi": pi}
                current = pi.get("code") if pi else None
                marker = home_marker.read(self.root)
                mode = mode_of(devices, marker)
                self._data = {**devices, "marker": marker and marker["home"], "mode": mode,
                              "needed": MODES.get(mode), "configured": bool(self.host), "home": home_of(devices),
                              "code_matches": None if current is None else current == mac["code"], "at": time.time()}
                self._at = self.clock()
            return self._data
