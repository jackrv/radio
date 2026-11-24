"""Оновлення коду Pi з панелі Mac: Pi бере з GitHub той самий коміт, що на Mac; лише файли профілю pi.
Збій перевірки повертає попередній код.

База, .env, архів і юніти systemd не зачіпаються (юніти — sudo sh deploy/hub/install.sh --units, якщо змінилися).
"""
import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from server import devices

FOLDER = "radio345-hub"

# Виконується на Pi стандартним python3. prepare: копія чинного коду; fetch: коміт із GitHub; finish: зайве, залежності, імпорт, юніти.
REMOTE = r'''
import hashlib, json, os, subprocess, sys, tarfile
root = os.path.join(os.path.expanduser("~"), sys.argv[1])
os.chdir(root)
backup = ".local-backups/code-before-update.tar.gz"
def names(path):
    try:
        with open(path) as f:
            return [l.strip().lstrip("/") for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []
def safe(name):
    return not os.path.isabs(name) and ".." not in name.split("/") and not name.split("/")[0].startswith(".local") and name != ".env"
def digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None
def restore():
    with open(backup + ".json") as f:
        old = json.load(f)
    for name in names("deploy/profiles/pi.files"):
        if safe(name) and name not in old["names"] and os.path.isfile(name):
            os.unlink(name)
    with tarfile.open(backup) as tar:
        tar.extractall(".", filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(".")
step = sys.argv[2]
if step == "prepare":
    os.makedirs(".local-backups", 0o700, exist_ok=True)
    have = [n for n in names("deploy/profiles/pi.files") if safe(n) and os.path.isfile(n)]
    with tarfile.open(backup + ".tmp", "w:gz") as tar:
        for n in have:
            tar.add(n)
    os.chmod(backup + ".tmp", 0o600)
    os.replace(backup + ".tmp", backup)
    with open(backup + ".json", "w") as f:
        json.dump({"names": have, "requirements": digest("requirements-hub.txt")}, f)
    print(json.dumps({"ok": True}))
elif step == "fetch":
    url, commit = sys.argv[3], sys.argv[4]
    cache, env = ".local-code.git", dict(os.environ, GIT_TERMINAL_PROMPT="0")
    def git(*args, **kw):
        return subprocess.run(["git", "--git-dir", cache, *args], env=env, stdin=subprocess.DEVNULL, capture_output=True, **kw)
    if not os.path.isdir(cache) and subprocess.run(["git", "init", "-q", "--bare", cache], capture_output=True).returncode:
        sys.exit("git")
    if git("fetch", "-q", "--depth", "1", url, commit).returncode:
        sys.exit("fetch")
    listing = git("show", commit + ":deploy/profiles/pi.files")
    wanted = [l.strip().lstrip("/") for l in listing.stdout.decode().splitlines() if l.strip() and not l.startswith("#")]
    if listing.returncode or not wanted or not all(safe(n) for n in wanted):
        sys.exit("manifest")
    archive = git("archive", "--format=tar", commit, "--", *wanted)
    if archive.returncode:
        sys.exit("manifest")
    import io
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
        if {m.name for m in members if m.isfile()} != set(wanted):
            sys.exit("manifest")
        try:
            tar.extractall(".", members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        except Exception:
            restore(); sys.exit("extract")
    print(json.dumps({"ok": True}))
elif step == "restore":
    restore()
    print(json.dumps({"ok": True}))
else:
    with open(backup + ".json") as f:
        old = json.load(f)
    new = set(names("deploy/profiles/pi.files"))
    if not new:
        sys.exit("manifest")
    if digest("requirements-hub.txt") != old["requirements"]:
        if subprocess.run([".venv/bin/pip", "install", "-q", "-r", "requirements-hub.txt"], stdin=subprocess.DEVNULL, capture_output=True).returncode:
            restore(); sys.exit("pip")
    modules = "server.radio_node,server.asr_service,server.api.local_dashboard,server.api.local_server,server.miniapp_tunnel"
    check = subprocess.run([".venv/bin/python", "-c", "import " + modules], capture_output=True, stdin=subprocess.DEVNULL)
    if check.returncode:
        restore(); sys.exit("import")
    for name in old["names"]:
        if name not in new and safe(name) and os.path.isfile(name):
            os.unlink(name)
    with open(".local-commit", "w") as f:
        f.write(sys.argv[3])
    user, units = os.environ.get("USER", ""), []
    for unit in ("node", "asr", "panel", "api", "tunnel"):
        installed = "/etc/systemd/system/radio345-%s.service" % unit
        if os.path.exists(installed):
            with open("deploy/hub/radio345-%s.service" % unit) as f:
                want = f.read().replace("@USER@", user).replace("@ROOT@", root)
            with open(installed) as f:
                if f.read() != want:
                    units.append(unit)
    print(json.dumps({"ok": True, "units": units}))
'''
ERRORS = {"pip": "Залежності не встановилися; повернуто попередній код", "import": "Новий код на Pi не завантажується; повернуто попередній",
          "manifest": "Пошкоджений список файлів у коміті", "git": "На Pi не працює git",
          "fetch": "Pi не отримала код із GitHub: перевірте інтернет", "extract": "Код не розпакувався; повернуто попередній"}


class UpdateError(Exception):
    """Текст показується оператору."""


class PiUpdate:
    def __init__(self, root, host, run=subprocess.run, control=devices.control_pi, fetch=devices.remote, sleep=time.sleep):
        self.root, self.host, self.run, self.control, self.fetch, self.sleep = Path(root), host, run, control, fetch, sleep
        self.state, self.thread, self.guard = {"phase": "idle"}, None, threading.Lock()

    def read(self):
        return {**self.state, "active": self.thread is not None and self.thread.is_alive()}

    def source(self):
        """(адреса GitHub, коміт) того, що на Mac; незапушене чи змінене — відмова."""
        def git(*args):
            done = self.run(["git", "-C", str(self.root), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            return done.stdout.strip() if done.returncode == 0 else None
        url, commit = git("remote", "get-url", "origin") or "", git("rev-parse", "HEAD") or ""
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", url) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise UpdateError("Не вдалося визначити коміт і адресу GitHub")
        if git("status", "--porcelain", "--untracked-files=normal") != "":
            raise UpdateError("На Mac є незакомічені зміни: спершу commit і push")
        if git("merge-base", "--is-ancestor", commit, "origin/main") is None:
            raise UpdateError("Коміт Mac ще не на GitHub: спершу push")
        return url, commit

    def start(self):
        with self.guard:
            if not self.host:
                raise UpdateError("Не налаштовано RADIO345_PI_SSH")
            if self.read()["active"]:
                raise UpdateError("Оновлення вже триває")
            url, commit = self.source()
            self.state = {"phase": "copying"}
            self.thread = threading.Thread(target=self.update, args=(url, commit), name="pi-update", daemon=True)
            self.thread.start()

    def _remote(self, step, *args, timeout=900):
        try:
            done = self.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", self.host,
                             " ".join(["python3 -I -", FOLDER, step, *map(shlex.quote, args)])], input=REMOTE, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError("Pi не відповіла вчасно") from None
        if done.returncode:
            last = (done.stderr or "").strip().splitlines()[-1:]
            raise UpdateError(ERRORS.get(last[0] if last else "", "SSH до Pi недоступний" if done.returncode == 255 else "Pi не виконала оновлення"))
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise UpdateError("Незрозуміла відповідь Pi") from None

    def update(self, url, commit):
        try:
            self._remote("prepare", timeout=120)
            self._remote("fetch", url, commit)
            self.state = {"phase": "checking"}
            result = self._remote("finish", commit)
            self.state = {"phase": "restarting"}
            alive = [n for n, p in (self.fetch(self.host).get("processes") or {}).items() if p.get("alive")]
            for process in alive:
                self.control(self.host, process, "restart")
            self.sleep(8)
            state = self.fetch(self.host).get("processes") or {}
            down = [n for n in alive if not state.get(n, {}).get("alive")]
            if down:
                raise UpdateError("Код оновлено, але не піднялися: " + ", ".join(down))
            self.state = {"phase": "done", "units": result.get("units") or [], "at": time.time()}
        except Exception as exc:
            text = str(exc) if isinstance(exc, (UpdateError, devices.Refused)) else f"Несподівана помилка: {type(exc).__name__}"
            self.state = {"phase": "failed", "error": text}
