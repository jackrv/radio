"""Один запуск на будь-якому пристрої: профіль → окремі процеси під наглядом.

    .venv/bin/python -m radio345 up --profile mac-dev              # усі процеси профілю
    .venv/bin/python -m radio345 up --profile mac-dev --only panel # лише вибрані
    .venv/bin/python -m radio345 check --profile pi                # що запуститься й чого бракує

Кожен процес окремий: падіння одного не зупиняє інші, він перезапускається з паузою.
На Pi процеси веде systemd (deploy/hub); тут — для Mac і розробки.
"""
import argparse
import os
import signal
import subprocess
import sys
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
MIN_PAUSE_S, MAX_PAUSE_S, STABLE_S, STOP_S = 2.0, 60.0, 60.0, 20.0


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
    def __init__(self, name, command, env, output, clock=time.monotonic):
        self.name, self.command, self.env, self.output, self.clock = name, command, env, output, clock
        self.process, self.pause, self.next_start, self.started, self.restarts = None, MIN_PAUSE_S, 0.0, 0.0, -1

    def tick(self):
        now = self.clock()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            if now - self.started >= STABLE_S:
                self.pause = MIN_PAUSE_S
            self.output(self.name, f"зупинився (код {code}); перезапуск через {self.pause:.0f} с")
            self.process, self.next_start = None, now + self.pause
            self.pause = min(self.pause * 2, MAX_PAUSE_S)
        if now >= self.next_start:
            self.started = now
            self.restarts += 1
            # Власна група: Ctrl+C іде лише наглядачеві, він зупиняє дітей по черзі.
            self.process = subprocess.Popen([sys.executable, "-u", *self.command], cwd=ROOT, env=self.env,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace", start_new_session=True)
            threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

    def _pump(self, process):
        for line in process.stdout:
            self.output(self.name, line.rstrip("\n"))

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


def supervise(children, stop, interval=0.5):
    try:
        while not stop.is_set():
            for child in children:
                child.tick()
            stop.wait(interval)
    finally:
        # Спершу ефір і розпізнавання, останньою — модель, щоб фрази встигли відкластися.
        for child in reversed(children):
            child.terminate()
        deadline = time.monotonic() + STOP_S
        for child in children:
            child.finish(deadline)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Radio 345: запуск процесів профілю")
    parser.add_argument("action", choices=("up", "check"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--only", nargs="+", default=(), metavar="ПРОЦЕС")
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    names = selected(profile, args.only)
    env = base_environment(profile)
    missing = missing_env(profile, env, names)
    if args.action == "check":
        print(f"{args.profile}: {profile['description']}")
        for name in names:
            print(f"  {name}: python {' '.join(COMMANDS[name])}")
        print(f"  бот: {'так' if profile.get('bot') else 'ні'}")
        for key in missing:
            print(f"  бракує в .env: {key} — {profile['required_env'][key]}")
        return 1 if missing else 0
    if missing:
        raise SystemExit("Бракує в .env: " + ", ".join(missing))
    lock = threading.Lock()

    def output(name, line):
        with lock:
            print(f"[{name}] {line}", flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *a: stop.set())
    supervise([Child(n, COMMANDS[n], process_environment(n, profile, env), output) for n in names], stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
