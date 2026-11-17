"""Власні процеси запускателя, канал зупинки та безпечна локальна діагностика."""

import fcntl
import json
import os
from pathlib import Path
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

GRACE_SECONDS = {"radio": 30.0, "ngrok": 5.0, "api": 5.0, "miniapp": 5.0, "admin": 5.0}
NAMES = frozenset(GRACE_SECONDS)


def lock_held(path):
    """Не створює, не видаляє і не перезаписує lock-файл."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def read_state(root):
    try:
        path = Path(root) / ".radio345-launcher/state.json"
        if path.stat().st_size > 32768:
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def managed_status(root):
    state = read_state(root)
    active = lock_held(Path(root) / ".radio345-launcher/lock")
    # Токен керування не потрапляє у вивід --status.
    controllers = state.get("controllers", {})
    if not isinstance(controllers, dict):
        controllers = {}
    reachable = False
    if active:
        try:
            fd = os.open(Path(root) / ".radio345-launcher/control.fifo",
                         os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            reachable = stat.S_ISFIFO(os.fstat(fd).st_mode)
            os.close(fd)
        except OSError:
            pass
    return {"managed": active, "launcher_pid": state.get("pid") if active else None,
            "launcher_reachable": reachable,
            "state": (state.get("state", "unknown") if reachable else "cleanup_pending") if active else "inactive",
            "controllers": {k: v for k, v in controllers.items()
                            if k in NAMES and type(v) is int},
            "stopped_services": [n for n in state.get("stopped_services", []) if n in NAMES] if active else []}


COMMANDS = ("stop", "restart", "receiver-stop", "receiver-start")
SERVICE_REQUESTS = ("receiver-stop", "receiver-start")


def send_command(root, command):
    """Передає команду запущеному лаунчеру через його канал і одразу повертається.

    False — керованого запуску немає. Чужі процеси не зачіпаються: команда діє
    лише з токеном поточного запуску.
    """
    if command not in COMMANDS:
        raise ValueError("Невідома команда")
    directory = Path(root) / ".radio345-launcher"
    if not lock_held(directory / "lock"):
        return False
    state = read_state(root)
    token = state.get("token")
    if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise RuntimeError("Немає перевіреного каналу керування цим запуском")
    path = directory / "control.fifo"
    if not stat.S_ISFIFO(path.lstat().st_mode):
        raise RuntimeError("Некоректний канал керування запускателем")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise RuntimeError("Запускатель уже зник; контролери завершують служби. Перевірте --status") from None
    try:
        os.write(fd, f"{command} {token}\n".encode())
    finally:
        os.close(fd)
    return token


def request_restart(root):
    """Лаунчер штатно зупиняє всі служби й запускає їх знову в тому самому вікні."""
    return bool(send_command(root, "restart"))


def request_stop(root, timeout=None):
    directory = Path(root) / ".radio345-launcher"
    token = send_command(root, "stop")
    if not token:
        return False
    deadline = time.monotonic() + (timeout if timeout is not None else sum(GRACE_SECONDS.values()) + 5)
    while lock_held(directory / "lock"):
        if read_state(root).get("token") != token:
            return True  # Старий запуск завершено; новий не чіпаємо.
        if time.monotonic() >= deadline:
            raise RuntimeError("Зупинка ще триває; перевірте --status. Чужі процеси не завершувались")
        time.sleep(0.1)
    return True


class LauncherSession:
    def __init__(self, root, names, *, log=print, grace=None):
        self.root = Path(root)
        self.directory = self.root / ".radio345-launcher"
        self.names = tuple(names)
        if not self.names or set(self.names) - NAMES or len(set(self.names)) != len(self.names):
            raise ValueError("Некоректний перелік служб")
        self.grace = {**GRACE_SECONDS, **(grace or {})}
        self.log = log
        self.token = secrets.token_hex(16)
        self.controllers = {}
        self.stopping = False
        self.restart_requested = False
        self.requests = []          # receiver-stop / receiver-start для головного циклу
        self.stopped_services = set()
        self.deadline = None
        self.lock_fd = self.watch_read = self.watch_write = self.control_fd = None
        self.done = {}
        self.handlers = {}
        self.thread = None
        self.thread_stop = threading.Event()
        self._registering = False
        self._stop_pending = False

    def _save(self, state):
        data = {"pid": os.getpid(), "token": self.token, "state": state, "controllers": self.controllers,
                "stopped_services": sorted(self.stopped_services)}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.directory, delete=False) as file:
                temporary = file.name
                json.dump(data, file)
            os.replace(temporary, self.directory / "state.json")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def __enter__(self):
        if self.directory.is_symlink():
            raise RuntimeError("Каталог керування не може бути символічним посиланням")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.lock_fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("Інший запускатель працює або його контролери ще завершують служби; перевірте --status") from None
        try:
            self.watch_read, self.watch_write = os.pipe()
            self.done = {name: list(os.pipe()) for name in self.names}
            fifo = self.directory / "control.fifo"
            if fifo.exists() or fifo.is_symlink():
                fifo.unlink()
            os.mkfifo(fifo, 0o600)
            self.control_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            self._save("starting")
            for signo in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.handlers[signo] = signal.getsignal(signo)
                signal.signal(signo, self._signal)
            self.thread = threading.Thread(target=self._control_loop, name="launcher-control", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _signal(self, *_):
        if not self.stopping:
            if self._registering:
                self._stop_pending = True
                return
            raise KeyboardInterrupt

    def _control_loop(self):
        buffer = b""
        while not self.thread_stop.is_set():
            if not select.select([self.control_fd], [], [], 0.1)[0]:
                continue
            buffer += os.read(self.control_fd, 1024)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if self.stopping:
                    continue
                if secrets.compare_digest(line, f"restart {self.token}".encode()):
                    self.restart_requested = True
                    os.kill(os.getpid(), signal.SIGTERM)
                elif secrets.compare_digest(line, f"stop {self.token}".encode()):
                    os.kill(os.getpid(), signal.SIGTERM)
                else:
                    for request in SERVICE_REQUESTS:
                        if secrets.compare_digest(line, f"{request} {self.token}".encode()):
                            self.requests.append(request)
            if len(buffer) > 1024:
                buffer = b""

    def command(self, name, target):
        index = self.names.index(name)
        own_done = self.done[name][1]
        after = self.done[self.names[index + 1]][0] if index + 1 < len(self.names) else None
        fds = [self.watch_read, own_done]
        command = [sys.executable, "-u", str(Path(__file__).with_name("process_guard.py")),
                   "--watch", str(self.watch_read), "--done", str(own_done),
                   "--grace", str(self.grace[name]),
                   "--signal", str(int(signal.SIGINT if name == "radio" else signal.SIGTERM))]
        if after is not None:
            command += ["--after", str(after)]
            fds.append(after)
        # Lease лишається, доки живий хоча б один контролер або його служба,
        # зокрема якщо API завершився раніше за решту.
        command += ["--lease", str(self.lock_fd)]
        fds.append(self.lock_fd)
        return command + ["--", *target], tuple(fds)

    def take_request(self):
        """Наступний запит на окрему службу або None (викликає головний цикл лаунчера)."""
        try:
            return self.requests.pop(0)
        except IndexError:
            return None

    def stop_service(self, process, name):
        """Зупиняє одну службу, решта працює: напр. приймач на час важкого прогону.

        Контролер отримує SIGTERM і штатно зупиняє службу (приймач — через SIGINT,
        з очікуванням черги Telegram), після строку — лише власну групу.
        Для повторного запуску служба отримує нову пару каналу завершення.
        """
        if process.poll() is None:
            os.kill(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=self.grace[name] + 5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for fd in self.done[name]:
            if fd is not None:
                os.close(fd)
        self.done[name] = list(os.pipe())
        self.controllers.pop(name, None)
        self.stopped_services.add(name)
        self._save("running")

    def register(self, name, process):
        self._registering = True
        try:
            self.controllers[name] = process.pid
            self.stopped_services.discard(name)
            os.close(self.done[name][1])
            self.done[name][1] = None
            self._save("running")
        finally:
            self._registering = False
        if self._stop_pending:
            raise KeyboardInterrupt

    def begin_shutdown(self):
        if self.stopping:
            return
        self.stopping = True
        self.deadline = time.monotonic() + sum(self.grace[n] for n in self.names) + 5
        try:
            self._save("stopping")
        except OSError:
            self.log("Не вдалося оновити стан запускателя; зупинка продовжується")
        if self.watch_write is not None:
            os.close(self.watch_write)
            self.watch_write = None
        # Неспробовані служби не повинні блокувати ланцюжок завершення.
        for pair in self.done.values():
            if pair[1] is not None:
                os.close(pair[1])
                pair[1] = None

    def wait_process(self, process, name):
        self.begin_shutdown()
        done = self.done[name][0]
        if process.poll() is not None and not select.select([done], [], [], 0)[0]:
            # Конкретна служба ще тримає успадкований pipe; її PGID зайнятий.
            self.log(f"Контролер {name} зник; зупиняю лише його власну групу")
            try:
                os.killpg(process.pid, signal.SIGINT if name == "radio" else signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not select.select([done], [], [], max(0, self.deadline - time.monotonic()))[0]:
                os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=max(0.1, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.log("Контролер не завершився вчасно; примусово завершую лише його власну групу")
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def __exit__(self, *_):
        if self.lock_fd is None:
            return
        complete = False
        try:
            self.begin_shutdown()
            pending = [pair[0] for pair in self.done.values()]
            while pending:
                ready = select.select(pending, [], [], max(0, self.deadline - time.monotonic()))[0]
                if not ready:
                    break
                pending = [fd for fd in pending if fd not in ready]
            complete = not pending
            try:
                self._save("stopped" if complete else "cleanup_pending")
            except OSError:
                self.log("Не вдалося записати підсумковий стан запускателя")
        finally:
            self.thread_stop.set()
            if self.thread is not None:
                self.thread.join(timeout=1)
            for signo, handler in self.handlers.items():
                signal.signal(signo, handler)
            fds = [self.watch_read, self.watch_write, self.control_fd, self.lock_fd]
            fds += [fd for pair in self.done.values() for fd in pair]
            for fd in fds:
                if fd is not None:
                    os.close(fd)
            self.lock_fd = None
        self.log("Зупинку завершено" if complete else "Зупинка ще триває; перевірте --status")
