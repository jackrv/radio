"""Один запуск на будь-якому пристрої: профіль → окремі процеси під наглядом. Команда — ./r345.

    ./r345 up                                      # RADIO345_PROFILE і RADIO345_ONLY з .env; дім на іншому пристрої — лише потрібне
    ./r345 up --profile mac-dev --only panel       # явно
    ./r345 status | stop                           # стан; зупинити все
    ./r345 restart [panel …]                       # перезапустити всі або вибрані процеси
    ./r345 check                                   # що запуститься й чого бракує
    ./r345 doctor                                  # перевірити все
    ./r345 pause-model [--minutes 120]             # віддати модель вузла лабораторії; resume-model — повернути

Кожен процес окремий: падіння одного не зупиняє інші, він перезапускається з паузою.
Якщо зникне сам наглядач, process_guard зупинить служби. На Pi процеси веде systemd (deploy/hub).
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from shared import conflict, home_marker
from shared.r345_control import Control

ROOT = Path(__file__).resolve().parent
PROFILES = ROOT / "deploy" / "profiles"
COMMANDS = {
    "worker": ("-m", "worker.asr_worker"),
    "node": ("-m", "server.radio_node"),
    "asr": ("-m", "server.asr_service"),
    "api": ("-m", "server.api.local_server"),
    "panel": ("-m", "server.api.local_dashboard"),
    "tunnel": ("-m", "server.miniapp_tunnel"),
}   # лабораторія — не служба: запускається окремими командами
MIN_PAUSE_S, MAX_PAUSE_S, STABLE_S = 2.0, 60.0, 60.0
GRACE_S = 15.0   # скільки служба має на штатне завершення; далі — примусово
UNITS = ("radio345-node", "radio345-panel", "radio345-asr", "radio345-api", "radio345-tunnel")


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
    sets = profile.get("sets", {})
    only = [p for word in only for p in sets.get(word, (word,))]   # назва набору або служба
    unknown = set(only) - set(names)
    if unknown:
        raise SystemExit(f"Профіль не має: {', '.join(sorted(unknown))}. Набори: {', '.join(sets) or 'немає'}")
    # manual (напр. публічний тунель) — лише коли назвали явно.
    return [p for p in names if (p in only if only else p not in profile.get("manual", ()))]


def default_processes(profile, root=None, environ=None):
    """`up` без параметрів: RADIO345_ONLY з .env. Дім на іншому пристрої — лише модель (решту не пустить conflict)."""
    if home_marker.foreign(root or ROOT):
        away = [p for p in ("worker",) if p in profile["processes"]]
        if away:
            return selected(profile, away)
    wanted = (base_environment({}) if environ is None else environ).get("RADIO345_ONLY", "").split()
    return selected(profile, wanted)


def missing_env(profile, env, names=()):
    if names and "asr" not in names:
        return []
    return sorted(k for k in profile.get("required_env", {}) if not env.get(k, "").strip())


def blocked_text():
    home = home_marker.foreign(ROOT)
    return f"дім на {'Pi' if home == 'pi' else 'Mac'}" if home else "зайнято на іншому пристрої"


class Child:
    def __init__(self, name, command, env, output, clock=time.monotonic, lease=None, wanted=True):
        self.name, self.command, self.env, self.output, self.clock, self.lease = name, command, env, output, clock, lease
        self.process, self.pause, self.next_start, self.started, self.restarts = None, MIN_PAUSE_S, 0.0, 0.0, -1
        self.watch = self.done = None
        self.wanted = wanted   # зупинений навмисно процес не піднімаємо
        self.blocked = False   # зайнято на іншому пристрої: лише ручний перезапуск

    def tick(self):
        if self.blocked or not self.wanted:
            return
        now = self.clock()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            self._reap()
            if code == conflict.EXIT:
                self.blocked, self.process = True, None
                self.output(self.name, f"{blocked_text()}; повторів не буде: ./r345 restart {self.name}")
                return
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
        command = [sys.executable, "-u", str(ROOT / "shared" / "process_guard.py"), "--watch", str(watch_read),
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
        self.pause, self.next_start, self.blocked, self.wanted = MIN_PAUSE_S, 0.0, False, True

    def stop(self):
        self.wanted, self.blocked = False, False
        self.terminate()
        self.finish(time.monotonic() + GRACE_S + 3)

    def start(self):
        if not self.wanted or self.blocked:
            self.restart()

    def state(self):
        alive = self.process is not None and self.process.poll() is None
        return {"alive": alive, "pid": self.process.pid if alive else None, "restarts": max(self.restarts, 0),
                "wanted": self.wanted,
                **({"blocked": True} if self.blocked else {})}


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


MARK = {True: "●", False: "✗", None: "○"}
COLOR = {True: "32", False: "31", None: "90"}


def table(rows):
    """[(ok, назва, значення…)] — рівні колонки; колір лише в терміналі. ok: True, False, None (навмисно вимкнено)."""
    rows = [(r[0], *map(str, r[1:])) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(1, len(rows[0]))] if rows else []
    lines = []
    for ok, *cells in rows:
        mark = MARK[ok]
        if sys.stdout.isatty():
            mark = f"\x1b[{COLOR[ok]}m{mark}\x1b[0m"
        lines.append(f"  {mark} " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip())
    return "\n".join(lines)


def service_rows(processes):
    for name, item in processes.items():
        wanted = item.get("wanted", True)
        text = ("працює" if item["alive"] else blocked_text() if item.get("blocked") else "не працює" if wanted else "зупинено")
        yield (True if item["alive"] else False if wanted else None, name, text,
               f"перезапусків {item['restarts']}" if item["restarts"] else "")


def show_status(control, title="r345"):
    units = systemd_units()
    if units:
        print("Служби systemd")
        print(table((True if state == "active" else None if state == "inactive" else False, unit.removeprefix("radio345-"),
                     {"active": "працює", "inactive": "зупинено"}.get(state, state)) for unit, state in units.items()))
    data = control.read()
    if data is None:
        if control.held():
            print("r345 завершується")
        elif not units:
            print("r345 не запущено")
        return 0 if units and not control.held() else 3
    print(f"{title}: профіль {data['profile']}, PID {data['pid']}")
    print(table(service_rows(data["processes"])))
    return 0 if all(p["alive"] for p in data["processes"].values() if p.get("wanted", True)) else 1


def stop_all(control, timeout=GRACE_S + 10):
    data = control.read()
    if data is None:
        print("r345 не відповідає" if control.held() else "r345 не запущено")
        return 3 if control.held() else 0
    control.request_stop()
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
    names = list(names) or [n for n, p in data["processes"].items() if p.get("wanted", True)]
    before = {n: data["processes"][n]["pid"] for n in names}
    control.request("restart", names)
    deadline = time.monotonic() + timeout * max(1, len(names))
    while time.monotonic() < deadline:
        time.sleep(0.3)
        now = control.read()
        if now and all(now["processes"][n]["alive"] and now["processes"][n]["pid"] != before[n] for n in names):
            print("Перезапущено: " + ", ".join(names))
            return 0
    print("Перезапуск не підтверджено: ./r345 status")
    return 1


def lock_root():
    from shared.model_pause import model_root
    return model_root(ROOT, base_environment({}))


def pause_model(minutes, root=ROOT, timeout=90.0):
    from shared.model_pause import MAX_PAUSE_S, ModelPause
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
    from shared.model_pause import ModelPause
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


def capture(run, command):
    try:
        return run(command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def doctor(run=subprocess.run):
    """Лише читання. Pi й iPhone — з Mac; на Pi — її служби, база й діагностика."""
    show_status(Control(), "Служби")
    rows = data_rows(run)
    if sys.platform == "darwin":
        rows += mac_rows(run)
    print("\nСтан")
    print(table(rows))
    return 0 if all(ok is not False for ok, *_ in rows) else 1


def data_rows(run):
    """База, діагностика й ефір — того пристрою, де дім: на Mac застигла копія була б хибною тривогою."""
    command, where = [sys.executable, "-m", "tools.diagnose", "--rows"], ""
    if sys.platform == "darwin" and home_marker.foreign(ROOT) == "pi":
        from server import devices
        host = devices.pi_host(base_environment({}))
        if not host:
            return [(False, "Дані Pi", "не налаштовано RADIO345_PI_SSH")]
        where = " Pi"
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host,
                   "cd radio345-hub && .venv/bin/python -m tools.diagnose --rows"]
    try:
        return [(ok, name + where, value) for name, value, ok in json.loads(capture(run, command))]
    except ValueError:
        return [(False, "Дані Pi" if where else "База", "перевірка не виконалася")]


def mac_rows(run):
    from server import devices
    from shared import pull_backup
    rows, host = [], devices.pi_host(base_environment({}))
    if host:
        state = devices.remote(host)
        if state.get("reachable"):
            on = [n for n, p in state["processes"].items() if p["alive"]]
            same = state.get("code") == devices.code_hash()
            rows += [(True, "Pi", "працює: " + (", ".join(on) or "нічого")),
                     (True if same else None, "Код на Pi", "як на Mac" if same else "інший: «Оновити код Pi» в панелі")]
        else:
            rows.append((False, "Pi", state.get("error", "недоступна")))
    text = capture(run, ["/bin/bash", str(ROOT / "tools" / "ios_reinstall.sh"), "--status"])
    ios = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
    if "Підпис діє до" in ios:
        rows.append((True, "iPhone", f"підпис до {ios['Підпис діє до']}"))
    if "Агент" in ios:
        rows.append((ios["Агент"] == "увімкнено", "Продовження підпису", ios["Агент"]))
    info = pull_backup.overview()
    rows.append((True if info["count"] else None, "Копії Pi", f"{info['count']} з {info['keep']}"
                 + (f", остання {info['age_h']:.0f} год тому" if info["age_h"] is not None else "")))
    return rows


def service(switch):
    if sys.platform != "darwin":
        print("На Pi служби веде systemd: sudo sh deploy/hub/install.sh")
        return 1
    from shared import mac_service
    if switch == "status":
        print("Служба: " + ("увімкнено" if mac_service.enabled() else "вимкнено") + f"; журнал {mac_service.log_path(ROOT).relative_to(ROOT)}")
        return 0
    try:
        if switch == "on":
            mac_service.on(ROOT, Control().held())
            print("Служба увімкнена: запускається сама після входу в систему. Стан: ./r345 status, вимкнути: ./r345 service off")
        else:
            print("Службу зупинено й вимкнено" if mac_service.off() else "Служба й так вимкнена")
    except mac_service.Refused as exc:
        print(f"Не увімкнено: {exc}")
        return 1
    return 0


UPDATE_PHASES = {"copying": "беру код із GitHub", "checking": "перевіряю", "restarting": "перезапускаю служби"}


def follow(updater, refused):
    """Показати кроки оновлення; повертає кінцевий стан або None, якщо відмовлено."""
    try:
        updater.start()
    except refused as exc:
        print(f"  не оновлено: {exc}")
        return None
    shown = None
    while True:
        current = updater.read()
        if current["phase"] in UPDATE_PHASES and current["phase"] != shown:
            shown = current["phase"]
            print(f"  {UPDATE_PHASES[shown]}…", flush=True)
        if not current["active"]:
            return current
        time.sleep(0.5)


def update(target="all", mac=None, pi=None):
    """Mac — з GitHub, потім Pi — той самий коміт."""
    if sys.platform != "darwin":
        print("Оновлення запускають з Mac: ./r345 update там або кнопка в панелі")
        return 1
    code = 0
    if target in ("all", "mac"):
        code = update_mac(mac)
    if target in ("all", "pi") and code == 0:
        code = update_pi(pi)
    return code


def update_mac(updater=None):
    from server import mac_update
    print("Mac", flush=True)
    current = follow(updater or mac_update.MacUpdate(ROOT, control=Control()), mac_update.UpdateError)
    if current is None or current["phase"] != "done":
        if current is not None:
            print(f"  не оновлено: {current.get('error', 'невідома помилка')}")
        return 1
    print("  оновлено, служби перезапускаються" if current.get("changed") else "  вже найновіший код")
    if current.get("supervisor"):
        print("  змінився сам r345: ./r345 stop, потім ./r345 up")
    return 0


def update_pi(updater=None):
    """Код Pi = коміт Mac (з GitHub). Те саме, що кнопка «Оновити код Pi»."""
    from server import devices, pi_update
    host = devices.pi_host(base_environment({}))
    if not host:
        print("Pi\n  не налаштовано RADIO345_PI_SSH")
        return 0
    print("Pi", flush=True)
    updater = updater or pi_update.PiUpdate(ROOT, host)
    state = devices.remote(host)
    if state.get("reachable") and state.get("code") == devices.code_hash():
        print("  вже той самий код")
        return 0
    current = follow(updater, pi_update.UpdateError)
    if current is None or current["phase"] != "done":
        if current is not None:
            print(f"  не оновлено: {current.get('error', 'невідома помилка')}")
        return 1
    print("  оновлено")
    if current.get("units"):
        print("  на Pi один раз: sudo sh deploy/hub/install.sh --units")
    return 0


def start_pi_backups(output, daily=None, platform=None):
    """Mac: копія баз Pi раз на добу, доки дім на Pi. Веде сам r345: панель Mac для цього не потрібна."""
    if (platform or sys.platform) != "darwin":
        return None
    try:
        from server import devices
        from shared import pull_backup
        host = devices.pi_host(base_environment({}))
        return (daily or pull_backup.Daily)(ROOT, host).start() if host else None
    except Exception as exc:   # копії не мають завадити запуску служб
        output("копії Pi", f"не запущено: {type(exc).__name__}")
        return None


MOVE_STEPS = {"checking": "перевіряю", "stopping": "зупиняю служби", "copying": "копіюю базу", "installing": "записую базу",
              "starting": "запускаю служби", "rolling_back": "повертаю, як було"}


def follow_move(mover, sleep=time.sleep):
    """Кроки перенесення до кінця; Ctrl+C його не перериває."""
    shown = None
    while True:
        try:
            data = mover.read()
            if data["phase"] != shown and data.get("active"):
                shown = data["phase"]
                print(f"  {MOVE_STEPS.get(shown, shown)}", flush=True)
            if not data.get("active"):
                return data
            sleep(0.5)
        except KeyboardInterrupt:
            print("  перенесення триває, дочекайтеся кінця", flush=True)


def home(target=None, yes=False, state=None, mover=None, ask=input, sleep=time.sleep):
    """Де працює система: показати, перенести дім або вимкнути все. Та сама логіка, що в панелі."""
    if sys.platform != "darwin" and state is None:
        print("Дім переносять з Mac: ./r345 home там")
        return 1
    from server import devices, home_move
    Busy, exclusive = home_move.Busy, home_move.exclusive
    names = {"pi": "Pi", "mac": "Mac"}
    if state is None:
        import config
        host = devices.pi_host(base_environment({}))
        state = devices.Devices(config.DB_PATH, ROOT, host=host)
        mover = home_move.HomeMove(ROOT, home_move.MacSide(ROOT), home_move.PiSide(host, ROOT) if host else None)
    now = state.read()
    current = now["marker"] or now["home"]
    move = mover.read()
    if target is None:
        print(f"Дім: {names.get(current, 'невідомо')}" + (" (усе зупинено)" if now["mode"] == "off" else ""))
        pi = now.get("pi")
        if pi is not None and not pi.get("reachable"):
            print(f"Pi: {pi.get('error', 'недоступна')}")
        if move.get("active"):
            print("Триває перенесення")
        elif move.get("interrupted"):
            print("Є перерване перенесення: ./r345 home rollback")
        print("Змінити: ./r345 home pi | mac | off")
        return 0

    def sure(text):
        if yes:
            return True
        try:
            return ask(f"{text} [y/N] ").strip().lower() in ("y", "yes", "т", "так")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
    try:
        if target == "rollback":
            if not sure("Повернути, як було до перерваного перенесення?"):
                return 1
            mover.rollback()
            return finish_move(follow_move(mover, sleep))
        if move.get("active") or move.get("interrupted"):
            print("Триває перенесення" if move.get("active") else "Є перерване перенесення: ./r345 home rollback")
            return 1
        if target == "off" or current == target:
            if target == "off" and not sure("Зупинити ефір, бота й розпізнавання на обох пристроях?"):
                return 1
            with exclusive(ROOT):
                note = state.apply_mode(target)
            print("Вимкнено" if target == "off" else f"Дім: {names[target]}; потрібне увімкнено, зайве зупинено")
            if note:
                print(note)
        else:
            check = mover.check(target)
            if check["problems"]:
                print("Не перенесено: " + "; ".join(check["problems"]))
                return 1
            for warning in check["warnings"]:
                print(f"  ! {warning}")
            if not sure(f"Перенести дім на {names[target]}? Бот, Mini App і ефір кілька хвилин не працюватимуть."):
                return 1
            mover.start(target, True)
            code = finish_move(follow_move(mover, sleep))
            if code:
                return code
        if target == "mac":   # на домі потрібна панель
            Control().request("start", ["panel"])
        return 0
    except (Busy, devices.Refused, home_move.MoveError) as exc:
        print(f"Не виконано: {exc}")
        return 1


def finish_move(data):
    if data["phase"] == "done":
        print(f"Готово, дім: {'Pi' if data['target'] == 'pi' else 'Mac'}")
        return 0
    print(("Усе повернуто, як було. " if data["phase"] == "rolled_back" else "Перевірте пристрої (./r345 doctor). ") + (data.get("error") or ""))
    return 1


HELP = """Radio 345

Щодня
  ./r345 up                 запустити; що саме — RADIO345_PROFILE і RADIO345_ONLY у .env
  ./r345 stop               зупинити все (або Ctrl+C)
  ./r345 status             що працює
  ./r345 restart [служба]   перезапустити: worker, node, asr, api, panel, tunnel
  ./r345 doctor             перевірити все: служби, база, Pi, підпис iPhone, копії

Де працює система (з Mac)
  ./r345 home               показати, де дім
  ./r345 home pi | mac      перенести дім; на тому самому домі — увімкнути потрібне
  ./r345 home off           зупинити все на обох

Рідко
  ./r345 update [mac | pi]  оновити код із GitHub: Mac, потім Pi
  ./r345 check              що запуститься й чого бракує
  ./r345 pause-model        віддати модель лабораторії (--minutes 120); resume-model — повернути
  ./r345 service on | off   на Mac: працювати службою без вікна термінала (проєкт — не в ~/Documents)
  ./r345 move-data          перенести робочі дані в .local-data

Явний запуск: ./r345 up --profile mac-dev --only worker panel
Подробиці команди: ./r345 <команда> --help. Усі команди: docs/commands.md
На Pi службами керує systemd і панель Mac: там корисні status і doctor."""


def main(argv=None):
    words = sys.argv[1:] if argv is None else argv
    if not words or words[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    parser = argparse.ArgumentParser(prog="r345", description="Radio 345: процеси цього пристрою")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, text in (("up", "запустити"), ("check", "що запуститься й чого бракує")):
        one = sub.add_parser(name, help=text)
        one.add_argument("--profile", help="типово RADIO345_PROFILE з .env")
        one.add_argument("--only", nargs="+", default=(), metavar="НАБІР|ПРОЦЕС")
    sub.add_parser("status", help="стан процесів")
    sub.add_parser("stop", help="зупинити все")
    sub.add_parser("restart", help="перезапустити все або вибране").add_argument("names", nargs="*", metavar="ПРОЦЕС")
    sub.add_parser("pause-model", help="віддати модель лабораторії").add_argument("--minutes", type=int, default=120)
    sub.add_parser("resume-model", help="повернути модель вузлу")
    sub.add_parser("service", help="служба macOS замість вікна термінала").add_argument("switch", choices=("on", "off", "status"))
    sub.add_parser("update", help="оновити код із GitHub: Mac, потім Pi").add_argument("target", nargs="?", choices=("all", "mac", "pi"), default="all")
    where = sub.add_parser("home", help="де працює система: показати, перенести, вимкнути")
    where.add_argument("target", nargs="?", choices=("pi", "mac", "off", "rollback"))
    where.add_argument("--yes", action="store_true", help="без запитань")
    sub.add_parser("doctor", help="усе одним поглядом: служби, база, Pi, підпис iPhone, копії")
    sub.add_parser("move-data", help="перенести робочі дані в .local-data (служби зупинено)")
    args = parser.parse_args(argv)
    if args.action == "pause-model":
        return pause_model(args.minutes, lock_root())
    if args.action == "resume-model":
        return resume_model(lock_root())
    if args.action == "service":
        return service(args.switch)
    if args.action == "update":
        return update(args.target)
    if args.action == "home":
        return home(args.target, args.yes)
    if args.action == "doctor":
        return doctor()
    if args.action == "move-data":
        from shared import move_data
        if any(state in ("active", "activating") for state in systemd_units().values()):
            print("Спершу: sudo systemctl stop " + " ".join(systemd_units()))
            return 1
        try:
            done = move_data.move(ROOT)
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            print(f"Не перенесено: {exc}")
            return 1
        print(f"Перенесено: {len(done)}" if done else "Переносити нічого")
        return 0
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
    names = selected(profile, args.only) if args.only or args.profile else default_processes(profile)
    env = base_environment(profile)
    missing = missing_env(profile, env, names)
    if args.action == "check":
        print(f"{name}: {profile['description']}")
        for process in names:
            print(f"  {process}: python {' '.join(COMMANDS[process])}")
        for key, processes in profile.get("sets", {}).items():
            print(f"  набір {key}: {' '.join(processes)}")
        print(f"  бот: {'так' if profile.get('bot') else 'ні'}")
        for key in missing:
            print(f"  бракує в .env: {key} — {profile['required_env'][key]}")
        return 1 if missing else 0
    if missing:
        raise SystemExit("Бракує в .env: " + ", ".join(missing))
    if any(state in ("active", "activating") for state in systemd_units().values()):
        raise SystemExit("Службами керує systemd: спершу sudo systemctl stop " + " ".join(systemd_units()))
    in_service = os.environ.get("RADIO345_SERVICE") == "1"
    if in_service:
        from shared import mac_service
        mac_service.trim_log(ROOT)
        if control.held():
            print("r345 уже працює; служба не повторює спроб")
            return 0   # код 0: launchd не піднімає знову
    control.acquire()
    lock = threading.Lock()

    def output(process, line):
        with lock:
            print(f"[{process}] {line}", flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *a: stop.set())
    children = [Child(n, COMMANDS[n], process_environment(n, profile, env), output, lease=control.lock_fd, wanted=n in names)
                for n in COMMANDS if n in profile["processes"]]   # решту профілю можна запустити з панелі

    def on_tick():
        control.take_requests(children)
        control.save(name, children)

    start_pi_backups(output)
    try:
        supervise(children, stop, on_tick=on_tick)
    finally:
        control.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
