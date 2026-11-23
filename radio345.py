"""Один запуск на будь-якому пристрої: профіль → окремі процеси під наглядом. Команда — ./r345.

    ./r345 up [--profile mac-dev] [--only panel]   # профіль типово з RADIO345_PROFILE у .env
    ./r345 status | stop                           # стан; зупинити все
    ./r345 restart [panel …]                       # перезапустити всі або вибрані процеси
    ./r345 check                                   # що запуститься й чого бракує
    ./r345 pause-model [--minutes 120]             # віддати модель вузла лабораторії; resume-model — повернути

Кожен процес окремий: падіння одного не зупиняє інші, він перезапускається з паузою.
Якщо зникне сам наглядач, process_guard зупинить служби. Замок спільний зі start_radio345.py,
тож обидва лаунчери водночас не працюють. На Pi процеси веде systemd (deploy/hub).
"""
import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES = ROOT / "deploy" / "profiles"
COMMANDS = {
    "worker": ("-m", "services.asr_worker"),
    "node": ("-m", "services.radio_node"),
    "asr": ("-m", "services.asr_service"),
    "api": ("-m", "api.local_server"),
    "panel": ("-m", "api.local_dashboard"),
}   # лабораторія — не служба: запускається окремими командами
MIN_PAUSE_S, MAX_PAUSE_S, STABLE_S = 2.0, 60.0, 60.0
GRACE_S = 15.0   # скільки служба має на штатне завершення; далі — примусово
CONTROL = ROOT / ".radio345-launcher"
UNITS = ("radio345-node", "radio345-panel", "radio345-asr")


def load_profile(name):
    path = PROFILES / f"{name}.toml"
    if not path.is_file():
        raise SystemExit(f"Немає профілю {name}. Є: {', '.join(sorted(p.stem for p in PROFILES.glob('*.toml')))}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def base_environment(profile, environ=None, dotenv=None):
    """Пріоритет: оточення → .env → типові значення профілю."""
    if dotenv is None:
        from dotenv import dotenv_values
        dotenv = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
    return {**profile.get("env", {}), **dotenv, **(os.environ if environ is None else environ)}


def process_environment(name, profile, env):
    env = dict(env)
    if name == "asr" and not profile.get("bot"):
        env["TELEGRAM_BOT_TOKEN"] = ""   # порожнє значення не дає .env повернути токен: бот лише в профілі з bot = true
    return env


def selected(profile, only=()):
    names = [p for p in COMMANDS if p in profile["processes"]]
    unknown = set(only) - set(names)
    if unknown:
        raise SystemExit(f"Профіль не має процесів: {', '.join(sorted(unknown))}")
    return [p for p in names if not only or p in only]


def missing_env(profile, env, names=()):
    if names and "asr" not in names:
        return []
    return sorted(k for k in profile.get("required_env", {}) if not env.get(k, "").strip())


class Child:
    def __init__(self, name, command, env, output, clock=time.monotonic, lease=None):
        self.name, self.command, self.env, self.output, self.clock, self.lease = name, command, env, output, clock, lease
        self.process, self.pause, self.next_start, self.started, self.restarts = None, MIN_PAUSE_S, 0.0, 0.0, -1
        self.watch = self.done = None

    def tick(self):
        now = self.clock()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            self._reap()
            if now - self.started >= STABLE_S:
                self.pause = MIN_PAUSE_S
            self.output(self.name, f"зупинився (код {code}); перезапуск через {self.pause:.0f} с")
            self.process, self.next_start = None, now + self.pause
            self.pause = min(self.pause * 2, MAX_PAUSE_S)
        if now >= self.next_start:
            self._start(now)

    def _start(self, now):
        self.started = now
        self.restarts += 1
        watch_read, self.watch = os.pipe()   # EOF тут = наглядача немає, служба має зупинитися
        self.done, done_write = os.pipe()
        fds = [watch_read, done_write]
        command = [sys.executable, "-u", str(ROOT / "services" / "process_guard.py"), "--watch", str(watch_read),
                   "--done", str(done_write), "--grace", str(GRACE_S), "--signal", str(int(signal.SIGTERM))]
        if self.lease is not None:
            command += ["--lease", str(self.lease)]
            fds.append(self.lease)
        try:
            self.process = subprocess.Popen(
                [*command, "--", sys.executable, "-u", *self.command], cwd=ROOT, env=self.env, pass_fds=tuple(fds),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", start_new_session=True)
        finally:
            os.close(watch_read)
            os.close(done_write)
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

    def _pump(self, process):
        for line in process.stdout:
            self.output(self.name, line.rstrip("\n"))

    def _reap(self):
        # Контролер міг загинути раніше за службу: прибрати залишок його власної групи.
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for attr in ("watch", "done"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    def terminate(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    def finish(self, deadline):
        if self.process is None:
            return
        try:
            self.process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.output(self.name, "не завершився вчасно; примусова зупинка")
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(5)
        self._reap()
        self.process = None

    def restart(self):
        """Навмисний перезапуск: без паузи й без зростання затримки."""
        self.terminate()
        self.finish(time.monotonic() + GRACE_S + 3)
        self.pause, self.next_start = MIN_PAUSE_S, 0.0

    def state(self):
        alive = self.process is not None and self.process.poll() is None
        return {"alive": alive, "pid": self.process.pid if alive else None, "restarts": max(self.restarts, 0)}


def supervise(children, stop, interval=0.5, on_tick=lambda: None):
    try:
        while not stop.is_set():
            for child in children:
                child.tick()
            on_tick()
            stop.wait(interval)
    finally:
        for child in reversed(children):   # модель — останньою
            child.terminate()
        deadline = time.monotonic() + GRACE_S + 3
        for child in children:
            child.finish(deadline)


class Control:
    """Замок, стан і запити в .radio345-launcher/ (права 0700)."""

    def __init__(self, directory=None):
        self.directory = Path(directory or CONTROL)
        self.lock_path = self.directory / "lock"
        self.state_path = self.directory / "r345.json"
        self.lock_fd, self._last = None, None

    def acquire(self):
        if self.directory.is_symlink():
            raise SystemExit("Каталог керування не може бути символічним посиланням")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SystemExit("Radio 345 уже запущено (або ще завершується): ./r345 status") from None
        self.lock_fd = fd
        # Залишки старого лаунчера не мають виглядати як чинний канал чи «зупинений приймач».
        for stale in ("state.json", "control.fifo"):
            (self.directory / stale).unlink(missing_ok=True)
        for old in self.directory.glob("r345-restart-*"):
            old.unlink(missing_ok=True)

    def held(self):
        from services.launcher_lifecycle import lock_held
        return lock_held(self.lock_path)

    def save(self, profile, children):
        data = {"pid": os.getpid(), "profile": profile, "processes": {c.name: c.state() for c in children}}
        if data == self._last:
            return
        fd, temp = tempfile.mkstemp(dir=self.directory)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
        os.replace(temp, self.state_path)
        self._last = data

    def read(self):
        """Стан живого r345 або None (не запущено чи працює старий лаунчер)."""
        if not self.held():
            return None
        try:
            data = json.loads(self.state_path.read_text())
            pid = data["pid"]
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return data if type(pid) is int and "radio345 up" in command else None

    def request_restart(self, names):
        for name in names:
            fd = os.open(self.directory / f"r345-restart-{name}", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    def take_restarts(self, children):
        for child in children:
            path = self.directory / f"r345-restart-{child.name}"
            if path.exists():
                path.unlink(missing_ok=True)
                child.output(child.name, "перезапуск за командою")
                child.restart()

    def release(self):
        self.state_path.unlink(missing_ok=True)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def systemd_units():
    """Стан служб systemd на Pi або {}, якщо їх там немає."""
    if not shutil.which("systemctl"):
        return {}
    result = {}
    for unit in UNITS:
        if Path(f"/etc/systemd/system/{unit}.service").exists():
            done = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            result[unit] = done.stdout.strip() or "unknown"
    return result


def show_status(control):
    for unit, state in systemd_units().items():
        print(f"{unit} (systemd): {state}")
    data = control.read()
    if data is None:
        print("Працює старий лаунчер start_radio345.py" if control.held() else "r345 не запущено")
        return 3
    print(f"r345 працює: профіль {data['profile']}, PID {data['pid']}")
    for name, item in data["processes"].items():
        print(f"  {name}: {'працює' if item['alive'] else 'не працює'}, перезапусків {item['restarts']}")
    return 0 if all(p["alive"] for p in data["processes"].values()) else 1


def stop_all(control, timeout=GRACE_S + 10):
    data = control.read()
    if data is None:
        print("Працює старий лаунчер: зупиніть його start_radio345.py --stop" if control.held() else "r345 не запущено")
        return 3 if control.held() else 0
    os.kill(data["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while control.held():
        if time.monotonic() > deadline:
            print("Не зупинився вчасно")
            return 1
        time.sleep(0.2)
    print("Зупинено")
    return 0


def restart(control, names, timeout=GRACE_S + 20):
    data = control.read()
    if data is None:
        print("r345 не запущено")
        return 3
    unknown = set(names) - set(data["processes"])
    if unknown:
        print("Не запущено: " + ", ".join(sorted(unknown)))
        return 2
    names = list(names) or list(data["processes"])
    before = {n: data["processes"][n]["pid"] for n in names}
    control.request_restart(names)
    deadline = time.monotonic() + timeout * max(1, len(names))
    while time.monotonic() < deadline:
        time.sleep(0.3)
        now = control.read()
        if now and all(now["processes"][n]["alive"] and now["processes"][n]["pid"] != before[n] for n in names):
            print("Перезапущено: " + ", ".join(names))
            return 0
    print("Перезапуск не підтверджено: ./r345 status")
    return 1


def pause_model(minutes, root=ROOT, timeout=90.0):
    from services.model_pause import MAX_PAUSE_S, ModelPause
    pause = ModelPause(root)
    if not pause.worker_running():
        print("Вузол розшифровки не запущено: модель у ньому не тримається")
        return 3
    if not 0 < minutes * 60 <= MAX_PAUSE_S - 1:
        print(f"Строк — від 1 до {MAX_PAUSE_S // 60 - 1} хв")
        return 2
    pause.start(time.time() + minutes * 60)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pause.ready():
            print(f"Модель вільна на {minutes} хв; фрази відкладаються. Повернути: ./r345 resume-model")
            return 0
        time.sleep(0.3)
    pause.stop()
    print("Вузол не підтвердив звільнення моделі; запит скасовано")
    return 1


def resume_model(root=ROOT, timeout=120.0):
    from services.model_pause import ModelPause
    pause = ModelPause(root)
    pause.stop()
    if not pause.worker_running():
        print("Вузол розшифровки не запущено")
        return 3
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pause.restored():
            print("Модель повернуто, розпізнавання працює")
            return 0
        time.sleep(0.3)
    print("Модель ще зайнята (лабораторія?): вузол забере її, щойно звільниться")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="r345", description="Radio 345: процеси цього пристрою")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, text in (("up", "запустити"), ("check", "що запуститься й чого бракує")):
        one = sub.add_parser(name, help=text)
        one.add_argument("--profile", help="типово RADIO345_PROFILE з .env")
        one.add_argument("--only", nargs="+", default=(), metavar="ПРОЦЕС")
    sub.add_parser("status", help="стан процесів")
    sub.add_parser("stop", help="зупинити все")
    sub.add_parser("restart", help="перезапустити все або вибране").add_argument("names", nargs="*", metavar="ПРОЦЕС")
    sub.add_parser("pause-model", help="віддати модель лабораторії").add_argument("--minutes", type=int, default=120)
    sub.add_parser("resume-model", help="повернути модель вузлу")
    args = parser.parse_args(argv)
    if args.action == "pause-model":
        return pause_model(args.minutes)
    if args.action == "resume-model":
        return resume_model()
    control = Control()
    if args.action == "status":
        return show_status(control)
    if args.action == "stop":
        return stop_all(control)
    if args.action == "restart":
        return restart(control, args.names)
    name = args.profile or base_environment({}).get("RADIO345_PROFILE", "").strip()
    if not name:
        raise SystemExit("Не задано профіль: RADIO345_PROFILE у .env або --profile")
    profile = load_profile(name)
    names = selected(profile, args.only)
    env = base_environment(profile)
    missing = missing_env(profile, env, names)
    if args.action == "check":
        print(f"{name}: {profile['description']}")
        for process in names:
            print(f"  {process}: python {' '.join(COMMANDS[process])}")
        print(f"  бот: {'так' if profile.get('bot') else 'ні'}")
        for key in missing:
            print(f"  бракує в .env: {key} — {profile['required_env'][key]}")
        return 1 if missing else 0
    if missing:
        raise SystemExit("Бракує в .env: " + ", ".join(missing))
    if any(state in ("active", "activating") for state in systemd_units().values()):
        raise SystemExit("Службами керує systemd: спершу sudo systemctl stop " + " ".join(systemd_units()))
    control.acquire()
    lock = threading.Lock()

    def output(process, line):
        with lock:
            print(f"[{process}] {line}", flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *a: stop.set())
    children = [Child(n, COMMANDS[n], process_environment(n, profile, env), output, lease=control.lock_fd) for n in names]

    def on_tick():
        control.take_restarts(children)
        control.save(name, children)

    try:
        supervise(children, stop, on_tick=on_tick)
    finally:
        control.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
