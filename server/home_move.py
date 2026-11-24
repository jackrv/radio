"""Перенесення «дому» (база, бот, Mini App, ефір) між Mac і Pi. Керує лише Mac.

Порядок: перевірки → зупинка джерела → знімок бази → передавання й звірка → збереження старої бази цілі →
запис → позначки дому → запуск цілі. Збій на будь-якому кроці повертає джерело.
Живу базу не підміняємо файлом: знімок заливається SQLite Backup API, тож відкрита панель не заважає.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from server import devices
from shared import home_marker
from shared.busy import Busy, exclusive
from shared.instance_lock import OWNER_LOCK, acquire_instance_lock
from shared.paths import REMOTE_PLACE, place

FILES = ("events.db", "index.sqlite3")
STOP_ORDER = ("node", "asr", "tunnel", "api")
START_ORDER = ("api", "asr", "tunnel", "node")
DRAIN_S, STOP_S, START_S, STABLE_S = 90.0, 60.0, 90.0, 25.0
STAGING = ".import-move"


class MoveError(Exception):
    """Текст показується оператору."""


def live_path(root, name):
    return place(root, name) if name == "events.db" else place(root, ".local-recordings") / name


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_database(source, target):
    """Знімок source → target через Backup API; target може бути відкритий іншими з'єднаннями."""
    a = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        b = sqlite3.connect(str(target), timeout=30)
        try:
            os.chmod(target, 0o600)
            b.execute("PRAGMA busy_timeout=30000")
            a.backup(b)
            if b.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise MoveError("Копія бази не пройшла перевірку цілісності")
        finally:
            b.close()
    finally:
        a.close()


def install_local(root, folder, stamp):
    """Виконується на цілі (Mac напряму, Pi — цим самим текстом через ssh, див. REMOTE_INSTALL)."""
    backup = place(root, ".local-backups") / f"before-move-{stamp}"
    backup.mkdir(mode=0o700, parents=True)
    for name in FILES:
        live = live_path(root, name)
        if live.exists():
            copy_database(live, backup / name)
        live.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        copy_database(folder / name, live)
    return backup


REMOTE_INSTALL = REMOTE_PLACE + r'''
import fcntl, hashlib, json, os, shutil, sqlite3, sys, tempfile
root = os.path.join(os.path.expanduser("~"), sys.argv[1])
os.makedirs(os.path.join(root, ".local-data"), 0o700, exist_ok=True)
stamp, epoch, sums = sys.argv[2], int(sys.argv[3]), json.loads(sys.argv[4])
staging = os.path.join(place(root, ".import-move"), stamp)
files = {"events.db": place(root, "events.db"), "index.sqlite3": os.path.join(place(root, ".local-recordings"), "index.sqlite3")}
def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
def copy(source, target):
    a = sqlite3.connect("file:%s?mode=ro" % source, uri=True, timeout=30)
    b = sqlite3.connect(target, timeout=30)
    os.chmod(target, 0o600)
    b.execute("PRAGMA busy_timeout=30000")
    a.backup(b)
    ok = b.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    b.close(); a.close()
    if not ok:
        sys.exit("integrity")
for name in files:
    if digest(os.path.join(staging, name)) != sums[name]:
        sys.exit("checksum")
lock = os.open(place(root, ".radio345-owner.lock"), os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("busy")
backup = os.path.join(place(root, ".local-backups"), "before-move-" + stamp)
os.makedirs(backup, 0o700)
for name, relative in files.items():
    live = relative
    if os.path.exists(live):
        copy(live, os.path.join(backup, name))
    os.makedirs(os.path.dirname(live), 0o700, exist_ok=True)
    copy(os.path.join(staging, name), live)
shutil.rmtree(staging)
fd, temp = tempfile.mkstemp(dir=root)
with os.fdopen(fd, "w") as out:
    json.dump({"home": "pi", "epoch": epoch}, out)
os.replace(temp, place(root, ".local-home.json"))
print("installed")
'''

REMOTE_MARK = REMOTE_PLACE + r'''
import json, os, sys, tempfile
root = os.path.join(os.path.expanduser("~"), sys.argv[1])
os.makedirs(os.path.join(root, ".local-data"), 0o700, exist_ok=True)
fd, temp = tempfile.mkstemp(dir=root)
with os.fdopen(fd, "w") as out:
    json.dump({"home": sys.argv[2], "epoch": int(sys.argv[3])}, out)
os.replace(temp, place(root, ".local-home.json"))
print("marked")
'''


class MacSide:
    name = "mac"

    def __init__(self, root, sleep=time.sleep):
        self.root, self.sleep = Path(root), sleep

    def state(self):
        from shared.r345_control import Control
        running = Control(place(self.root, ".radio345-launcher")).read()
        if running is None:
            return {"reachable": False, "error": "На Mac не працює r345"}
        spool = place(self.root, ".local-segment-spool")
        return {"reachable": True, "processes": running["processes"],
                "queue": sum(1 for _ in spool.glob("*.seg")) if spool.is_dir() else 0, "audio": True,
                "disk_free": shutil.disk_usage(self.root).free}

    def control(self, process, verb):
        devices.control_mac(process, verb, self.root)

    def snapshot(self, folder):
        for name in FILES:
            copy_database(live_path(self.root, name), folder / name)

    def install(self, folder, stamp, epoch):
        lock = acquire_instance_lock(place(self.root, OWNER_LOCK))
        if lock is None:
            raise MoveError("Розпізнавання на Mac ще тримає базу")
        try:
            install_local(self.root, folder, stamp)
            home_marker.write(self.root, "mac", epoch)
        finally:
            os.close(lock)

    def mark(self, home, epoch):
        home_marker.write(self.root, home, epoch)


class PiSide:
    name = "pi"

    def __init__(self, host, mac_root, folder="radio345-hub", run=subprocess.run, fetch=None, pull=None):
        self.host, self.mac_root, self.folder, self.run = host, Path(mac_root), folder, run
        self.fetch = fetch or devices.remote
        self.pull = pull

    def state(self):
        return self.fetch(self.host)

    def control(self, process, verb):
        devices.control_pi(self.host, process, verb)

    def _python(self, script, *args, timeout=600):
        import shlex
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", self.host,
                   "python3 -I - " + " ".join(shlex.quote(a) for a in (self.folder, *args))]
        try:
            done = self.run(command, input=script, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise MoveError("Pi не відповіла вчасно") from None
        if done.returncode:
            reason = {"busy": "Розпізнавання на Pi ще тримає базу", "checksum": "Файли на Pi пошкоджено під час передавання",
                      "integrity": "База на Pi не пройшла перевірку"}.get((done.stderr or "").strip().splitlines()[-1:][0]
                                                                          if (done.stderr or "").strip() else "")
            raise MoveError(reason or "Pi не виконала дію")
        return done.stdout

    def _where(self, name):
        """Абсолютний шлях на Pi: там старе або нове розташування."""
        out = self._python(REMOTE_PLACE + "import os,sys\nprint(place(os.path.join(os.path.expanduser('~'), sys.argv[1]), sys.argv[2]))\n",
                           name, timeout=60).strip()
        if not re.fullmatch(r"/[\w./-]+", out):
            raise MoveError("Незрозумілий шлях на Pi")
        return out

    def _rsync(self, *args, timeout=1800):
        try:
            done = self.run(["rsync", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10", *args],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise MoveError("Передавання перервано") from None
        if done.returncode:
            raise MoveError("Не вдалося передати файли")

    def snapshot(self, folder):
        from shared import pull_backup
        path = (self.pull or pull_backup.pull)(pull_backup.ssh_command(self.host, self.folder), pull_backup.OUT)
        for name in FILES:
            if not (path / name).is_file():
                raise MoveError("У копії з Pi немає індексу архіву")
            shutil.copyfile(path / name, folder / name)
            os.chmod(folder / name, 0o600)
        # Звук, записаний на Pi; наявні файли Mac не перезаписуються.
        self._rsync("-a", "--ignore-existing", "--include=*.wav", "--exclude=*",
                    f"{self.host}:{self._where('.local-recordings')}/", str(place(self.mac_root, ".local-recordings")) + "/")

    def install(self, folder, stamp, epoch):
        staging = f"{self._where(STAGING)}/{stamp}"
        self._python("import os,sys\nos.makedirs(sys.argv[2], 0o700)\n", staging)
        self._rsync("-a", *(str(folder / name) for name in FILES), f"{self.host}:{staging}/")
        self._python(REMOTE_INSTALL, stamp, str(epoch), json.dumps({n: sha256(folder / n) for n in FILES}))

    def mark(self, home, epoch):
        self._python(REMOTE_MARK, home, str(epoch), timeout=60)


class HomeMove:
    """Одне перенесення за раз; стан на диску переживає падіння панелі."""

    def __init__(self, root, mac, pi, sleep=time.sleep, clock=time.monotonic, backups=None):
        self.root, self.sides = Path(root), {"mac": mac, "pi": pi}
        self.sleep, self.clock, self.backups = sleep, clock, backups
        self.directory = place(self.root, ".local-move")
        self.state_path = self.directory / "state.json"
        self.thread, self.guard = None, threading.Lock()

    # ---------- стан ----------
    def read(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "idle"}
        data["active"] = self.thread is not None and self.thread.is_alive()
        data["interrupted"] = not data["active"] and data.get("phase") not in ("done", "failed", "rolled_back", "idle")
        return data

    def _save(self, **fields):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.current.update(fields, at=time.time())
        fd, temp = tempfile.mkstemp(dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(self.current, out, ensure_ascii=False)
        os.replace(temp, self.state_path)

    # ---------- перевірки ----------
    def check(self, target):
        """{"problems": […], "warnings": […], "emergency": bool} — без змін."""
        source = "pi" if target == "mac" else "mac"
        problems, warnings = [], []
        states = {n: s.state() if s else {"reachable": False, "error": "Не налаштовано RADIO345_PI_SSH"}
                  for n, s in self.sides.items()}
        if not states[target].get("reachable"):
            problems.append(states[target].get("error") or "Ціль недоступна")
            return {"problems": problems, "warnings": warnings, "emergency": False}
        alive = {n: {p for p, v in (s.get("processes") or {}).items() if v.get("alive")} for n, s in states.items()}
        if "asr" in alive[target]:
            problems.append("Бот і база вже працюють на цьому пристрої")
        emergency = not states[source].get("reachable") and source == "pi" and self.sides["pi"] is not None
        if emergency:
            copy, age = self._latest()
            warnings.append(f"Pi недоступна: буде взято копію бази, зроблену {age:.0f} год тому; новіші зміни з Pi втратяться"
                            if copy else "Pi недоступна й копій немає: Mac продовжить зі своєю базою")
        elif not states[source].get("reachable"):
            problems.append(states[source].get("error") or "Джерело недоступне")
        else:
            marker = home_marker.read(self.root)
            if "asr" not in alive[source]:
                # Зупинений дім теж можна перенести, якщо позначка каже, чия база справжня.
                if not marker:
                    problems.append("На джерелі не працює бот, а позначки дому немає: незрозуміло, чия база справжня")
                elif states[source].get("queue"):
                    warnings.append(f"У черзі {states[source]['queue']} нерозпізнаних фраз: вони не переїдуть. Щоб доробити їх, спершу увімкніть бота там")
            if marker and marker["home"] != source:
                problems.append(f"Позначка каже, що дім уже на {'Pi' if marker['home'] == 'pi' else 'Mac'}")
        need = sum(p.stat().st_size for p in (live_path(self.root, n) for n in FILES) if p.exists()) * 3
        for name in ("mac", "pi"):
            free = states[name].get("disk_free")
            if states[name].get("reachable") and free is not None and free < need + 500 * 1024 ** 2:
                problems.append(f"Замало місця на {'Pi' if name == 'pi' else 'Mac'}")
        if target == "pi":
            if states["pi"].get("audio") is False:
                warnings.append("На Pi немає звукового входу: ефіру не буде")
            if states["pi"].get("code") not in (None, devices.code_hash(self.root)):
                problems.append("Код на Pi відрізняється: спершу оновіть її")
            warnings.append("Звук архіву лишається на Mac; розмітка й тексти переїдуть")
        return {"problems": problems, "warnings": warnings, "emergency": emergency}

    def _latest(self):
        from shared import pull_backup
        return pull_backup.latest(self.backups or pull_backup.OUT)

    # ---------- перенесення ----------
    def start(self, target, accept_warnings=False):
        with self.guard:
            if self.thread is not None and self.thread.is_alive():
                raise MoveError("Перенесення вже триває")
            if self.read().get("interrupted"):
                raise MoveError("Є перерване перенесення: спершу поверніть")
            result = self.check(target)
            if result["problems"]:
                raise MoveError("; ".join(result["problems"]))
            if result["warnings"] and not accept_warnings:
                raise MoveError("Потрібне підтвердження попереджень")
            self.thread = threading.Thread(target=self.run, args=(target, result["emergency"]), name="home-move", daemon=True)
            self.thread.start()

    def run(self, target, emergency=False):
        self._exclusive(lambda: self._run(target, emergency), {"target": target, "source": "pi" if target == "mac" else "mac"})

    def _exclusive(self, work, fields):
        try:
            with exclusive(self.root):
                work()
        except Busy as exc:
            self.current = {**fields, "running": []}
            self._save(phase="failed", error=str(exc))

    def _run(self, target, emergency=False):
        source = "pi" if target == "mac" else "mac"
        a, b = self.sides[source], self.sides[target]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        marker = home_marker.read(self.root)
        epoch = (marker or {"epoch": 0})["epoch"] + 1
        self.current = {"id": stamp, "source": source, "target": target, "emergency": emergency, "epoch": epoch,
                        "running": [], "phase": "checking", "error": None}
        try:
            self._save()
            if emergency:
                running = ["node", "asr", "api"]
                self._save(running=running)
            else:
                state = a.state()
                running = [p for p in STOP_ORDER if (state.get("processes") or {}).get(p, {}).get("alive")]
                self._save(running=running, phase="stopping")
                self._stop(a, running)
            with tempfile.TemporaryDirectory(dir=self.directory, prefix="snap-") as temp:
                folder = Path(temp)
                self._save(phase="copying")
                if emergency:
                    copy, _ = self._latest()
                    if copy:
                        for name in FILES:
                            if (copy / name).is_file():
                                shutil.copyfile(copy / name, folder / name)
                else:
                    a.snapshot(folder)
                if all((folder / n).is_file() for n in FILES):
                    self._save(phase="installing")
                    b.install(folder, stamp, epoch)
                else:
                    b.mark(target, epoch)
            if not emergency:
                a.mark(target, epoch)   # джерело більше не дім: розпізнавання там не стартує
            self._save(phase="starting")
            audio = b.state().get("audio") is not False
            # Ефір — завжди, якщо цілі є чим слухати: на Pi без карти він не працював, та на Mac має повернутися.
            worker = (self.sides["mac"].state().get("processes") or {}).get("worker")
            if worker and not worker.get("alive"):   # модель — на Mac, де б не був дім
                self.sides["mac"].control("worker", "start")
            wanted = [p for p in START_ORDER if (p in running or p in ("asr", "api", "node")) and (p != "node" or audio)]
            self._start(b, wanted)
            self._save(phase="done")
        except Exception as exc:
            text = str(exc) if isinstance(exc, (MoveError, devices.Refused)) else f"Несподівана помилка: {type(exc).__name__}"
            self._save(phase="rolling_back", error=text)
            self._rollback()

    def rollback(self):
        """Повернути джерело після перерваного перенесення (наприклад, панель перезапустили)."""
        with self.guard:
            data = self.read()
            if not data.get("interrupted"):
                raise MoveError("Немає перерваного перенесення")
            self.current = {k: v for k, v in data.items() if k not in ("active", "interrupted")}
            self._save(phase="rolling_back", error=data.get("error") or "Перенесення перервано")
            self.thread = threading.Thread(target=lambda: self._exclusive(self._rollback, {"source": data.get("source"), "target": data.get("target")}), name="home-move", daemon=True)
            self.thread.start()

    def _rollback(self):
        source, target = self.current["source"], self.current["target"]
        a, b, epoch = self.sides[source], self.sides[target], self.current["epoch"] + 1
        notes = []
        if not b.state().get("reachable"):
            notes.append(f"{'Pi' if target == 'pi' else 'Mac'} не видно: стан її служб невідомий, другого бота зупинить перевірка конфлікту")
        for process in STOP_ORDER:
            try:
                if (b.state().get("processes") or {}).get(process, {}).get("alive"):
                    b.control(process, "stop")
            except Exception:
                notes.append(f"не вдалося зупинити {process} на цілі")
                break
        for side in (a, b):
            try:
                side.mark(source, epoch)
            except Exception:
                notes.append(f"позначку дому на {side.name} не оновлено")
        if not self.current.get("emergency"):
            try:
                self._start(a, [p for p in START_ORDER if p in self.current["running"]])
            except Exception as exc:
                notes.append(f"джерело не запустилося: {exc}" if isinstance(exc, (MoveError, devices.Refused)) else "джерело не запустилося")
        error = self.current.get("error") or ""
        self._save(phase="rolled_back" if not notes else "failed", error="; ".join([error, *notes]).strip("; "))

    def _alive(self, side):
        return {p for p, v in (side.state().get("processes") or {}).items() if v.get("alive")}

    def _stop(self, side, names):
        for process in names:
            side.control(process, "stop")
            if process == "node":   # доробити фрази, що вже в черзі
                deadline = self.clock() + DRAIN_S
                while side.state().get("queue") and self.clock() < deadline:
                    self.sleep(1.0)
        deadline = self.clock() + STOP_S
        while self._alive(side) & set(names):
            if self.clock() > deadline:
                raise MoveError("Служби джерела не зупинилися вчасно")
            self.sleep(1.0)

    def _start(self, side, names):
        for process in names:
            side.control(process, "start")
        deadline, since = self.clock() + START_S, None
        while True:
            state = side.state()
            processes = state.get("processes") or {}
            if any(processes.get(n, {}).get("blocked") for n in names):
                raise MoveError("Зайнято на іншому пристрої")
            if set(names) <= {p for p, v in processes.items() if v.get("alive")}:
                since = since if since is not None else self.clock()
                if self.clock() - since >= STABLE_S:
                    return
            else:
                since = None
            if self.clock() > deadline:
                raise MoveError("Служби не запустилися вчасно")
            self.sleep(2.0)
