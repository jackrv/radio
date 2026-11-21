#!/usr/bin/env python3
"""Запуск Radio 345: FastAPI, ngrok, приймач і необов’язковий Mini App preview.

    python start_radio345.py
    python start_radio345.py --local-miniapp
    python start_radio345.py --publish-pages
    python start_radio345.py --ngrok-url https://YOUR_DEV_DOMAIN.ngrok-free.app
    python start_radio345.py --status
    python start_radio345.py --stop
    python start_radio345.py --restart
    python start_radio345.py --receiver-stop    # лише приймач; панель, API й Mini App працюють
    python start_radio345.py --receiver-start

Ctrl+C, SIGTERM і SIGHUP зупиняють лише служби цього запуску.
"""

from __future__ import annotations

import argparse
import json
import logging
import atexit
import config
from core.console import ConsoleDashboard, child_event
from core.log_security import RedactingFormatter, archive_log_name
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from services.project_python import select_python
from services.admin_setup import ensure_admin
from services.dictionary_setup import ensure_dictionaries, interactive_console
from services.launcher_lifecycle import LauncherSession, lock_held, managed_status, request_restart, request_stop, send_command

ROOT = Path(__file__).resolve().parent
API_PORT = 8765
NGROK_INSPECT_PORT = 4040
MINIAPP_PORT = 8080


_console = None
_launcher_log = logging.getLogger("radio345.launcher")
_launcher_log.propagate = False


def start_console():
    global _console
    from core.log_security import PrivateRotatingFileHandler, TechnicalFileFormatter
    from services.private_files import protect_existing
    handler = PrivateRotatingFileHandler(ROOT / "launcher.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.namer = archive_log_name
    formatter = TechnicalFileFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S", launcher_setting("TELEGRAM_BOT_TOKEN"))
    for number in range(1, 6):
        protect_existing(archive_log_name(str(ROOT / "launcher.log") + f".{number}"))
    handler.setFormatter(formatter)
    _launcher_log.addHandler(handler)
    _launcher_log.setLevel(logging.INFO)
    def snapshot():
        return json.loads((ROOT / "diagnostics.json").read_text(encoding="utf-8"))
    from services.receiver_status import read_console_heartbeat
    _console = ConsoleDashboard(provider=snapshot, receiver_provider=lambda: read_console_heartbeat(config.DB_PATH))
    _console.receiver_requested_at = None
    _console.start()
    atexit.register(_console.close)


def close_console():
    global _console
    if _console is not None:
        _console.close()
        _console = None
    for handler in list(_launcher_log.handlers):
        handler.close()
        _launcher_log.removeHandler(handler)


def safe_message(message):
    formatter = RedactingFormatter("%(message)s", None, launcher_setting("TELEGRAM_BOT_TOKEN"))
    return ConsoleDashboard.clean(formatter.format(logging.makeLogRecord({"msg": message, "args": ()})))


def output(message: str) -> None:
    message = safe_message(message)
    level = logging.WARNING if "⚠" in message else logging.ERROR if message.startswith(("Помилка:", "Не вдалося")) else logging.INFO
    if _launcher_log.handlers:
        _launcher_log.log(level, message)
    if _console is not None:
        _console.add(message, level)
        return
    try:
        print(f"[launcher] {message}", flush=True)
    except (OSError, ValueError):
        pass


def service_failure(item, fallback):
    detail = getattr(item, "last_error", None)
    return detail if isinstance(detail, str) and detail else fallback


def launcher_setting(name: str, default: str = "") -> str:
    """Read only launcher-specific settings; don't echo any .env secrets."""
    if name in os.environ:
        return os.environ[name].strip()
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"\'')
    return default


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_python() -> str:
    return select_python(ROOT)


def read_expected_api_url() -> str | None:
    path = ROOT / "miniapp" / "index.html"
    if not path.is_file():
        return None
    html = path.read_text(encoding="utf-8")
    match = re.search(
        r'<meta\s+name=["\']radio345-api["\']\s+content=["\']([^"\']+)["\']',
        html,
    )
    return match.group(1).rstrip("/") if match else None


def valid_https_endpoint(url: str) -> str:
    """Validate an HTTPS endpoint before putting it in a public page."""
    if not isinstance(url, str):
        raise ValueError("HTTPS URL має бути рядком")
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in parsed.hostname.split(".")
        )
        or parsed.username or parsed.password
        or parsed.port is not None
        or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
        or len(url) > 255
    ):
        raise ValueError("Некоректний HTTPS URL ngrok")
    return f"https://{parsed.hostname.lower()}"


def update_miniapp_api_url(actual_url: str) -> bool:
    """Change only the local HTML meta tag; keep all other text unchanged."""
    actual_url = valid_https_endpoint(actual_url)
    path = ROOT / "miniapp" / "index.html"
    html = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'(<meta\s+name=["\']radio345-api["\']\s+content=["\'])([^"\']+)(["\'])'
    )
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise RuntimeError("У miniapp/index.html має бути рівно один meta radio345-api")
    old_url = matches[0].group(2).rstrip("/")
    if old_url == actual_url:
        return False
    changed = pattern.sub(lambda m: m.group(1) + actual_url + m.group(3), html, count=1)
    # Atomic replacement; don't risk a truncated HTML page on interruption.
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=".index-radio345-", delete=False,
        ) as tmp:
            temporary_name = tmp.name
            tmp.write(changed)
        os.chmod(temporary_name, path.stat().st_mode & 0o777)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    output(f"Локальний URL API Mini App оновлено: {old_url} → {actual_url}")
    return True


def git(*args: str, timeout: int = 40) -> str:
    run = subprocess.run(
        ["git", *args], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=timeout, check=False,
    )
    if run.returncode:
        # Never print full remote URLs/credentials that could appear in errors.
        raise RuntimeError(
            f"Команда git {args[0]} завершилася з кодом {run.returncode}; "
            "перевірте git status і налаштування доступу до origin вручну"
        )
    return run.stdout.strip()


def check_pages_publish_ready() -> None:
    if not shutil.which("git"):
        raise RuntimeError("git не знайдено в PATH")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
        raise RuntimeError("Засіб запуску має бути в корені git-репозиторію")
    branch = git("branch", "--show-current")
    if branch != "main":
        raise RuntimeError(f"Автопублікацію дозволено лише з гілки main (зараз {branch!r})")
    # Do not publish unrelated tracked/staged work. An earlier launcher run may
    # already have changed just the API URL in index.html; that is safe to reuse.
    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Є staged-зміни; автоматичну публікацію скасовано")
    changed_paths = git("diff", "--name-only").splitlines()
    if changed_paths and changed_paths != ["miniapp/index.html"]:
        raise RuntimeError("Є зміни, крім URL Mini App; автоматичну публікацію скасовано")
    if changed_paths:
        from_head = git("show", "HEAD:miniapp/index.html")
        current = (ROOT / "miniapp" / "index.html").read_text(encoding="utf-8")
        meta_pattern = re.compile(
            r'(<meta\s+name=["\']radio345-api["\']\s+content=["\'])([^"\']+)(["\'])'
        )
        if (
            len(meta_pattern.findall(from_head)) != 1
            or len(meta_pattern.findall(current)) != 1
            or meta_pattern.sub(r"\1API_URL\3", from_head).rstrip("\n") != meta_pattern.sub(r"\1API_URL\3", current).rstrip("\n")
        ):
            raise RuntimeError("index.html змінено не лише в URL; автоматичну публікацію скасовано")
    git("fetch", "origin", "main", timeout=60)
    if git("rev-parse", "HEAD") != git("rev-parse", "refs/remotes/origin/main"):
        raise RuntimeError("Локальна гілка та origin/main відрізняються; синхронізуйте їх вручну")


def publish_miniapp_url() -> None:
    """Commit & push only the HTML file, then GitHub Pages workflow deploys it."""
    git("add", "--", "miniapp/index.html")
    # --only prevents accidental inclusion of other staged files.
    git("commit", "--only", "-m", "Radio 345: sync public API endpoint", "--", "miniapp/index.html")
    git("push", "origin", "main", timeout=90)
    output("Новий URL надіслано в GitHub. GitHub Actions опублікує оновлення Mini App")


def wait_for_pages_url(expected_url: str, timeout_seconds: float = 90.0) -> bool:
    """Check published HTML without relying on a GitHub API token."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        query = f"https://jackrv.github.io/radio/?radio345_verify={time.time_ns()}"
        req = urllib.request.Request(query, headers={"Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                html = resp.read(250_000).decode("utf-8", errors="replace")
            if re.search(
                r'<meta\s+name=["\']radio345-api["\']\s+content=["\']'
                + re.escape(expected_url) + r'["\']', html,
            ):
                return True
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            pass
        time.sleep(3)
    return False


def read_ngrok_https_url() -> str | None:
    url = f"http://127.0.0.1:{NGROK_INSPECT_PORT}/api/tunnels"
    with urllib.request.urlopen(url, timeout=1.0) as response:
        doc = json.load(response)
    urls = [
        item.get("public_url", "")
        for item in doc.get("tunnels", [])
        if item.get("public_url", "").startswith("https://")
    ]
    return urls[0].rstrip("/") if urls else None


def api_reachable() -> bool:
    # Защищённый endpoint без initData ожидаемо возвращает 401.
    request = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}/api/status"
    )
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            return response.status in (200, 401, 403)
    except urllib.error.HTTPError as exc:
        return exc.code in (401, 403)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


class ManagedProcess:
    def __init__(self, name: str, command: list[str], *, session=None):
        self.name = name
        self.last_error = None
        self.session = session
        # Стан подій одного процесу: напр., чи перепідключається зараз ngrok.
        self._event_state = {}
        guarded, fds = session.command(name, command) if session is not None else (command, ())
        self.process = subprocess.Popen(
            guarded,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,  # власна група контролера та його служби
            pass_fds=fds,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "RADIO345_MANAGED_CONSOLE": "1"},
        )
        if session is not None:
            session.register(name, self.process)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        output(f"Запущено {name} (PID контролера {self.process.pid})")

    def _read_output(self) -> None:
        if self.process.stdout is None:
            return
        writable = True
        try:
            for line in self.process.stdout:
                if writable:
                    try:
                        _launcher_log.info("[%s] %s", self.name, line.rstrip())
                        event = child_event(self.name, line, getattr(self, "_event_state", None))
                        if event is not None and event[1] >= logging.ERROR:
                            self.last_error = safe_message(event[0])
                        if _console is not None:
                            if event is not None and event != getattr(self, "_last_console_event", None):
                                self._last_console_event = event
                                message, level = event
                                record = _launcher_log.makeRecord(_launcher_log.name, level, "", 0, message, (), None)
                                formatter = RedactingFormatter("%(message)s", None, launcher_setting("TELEGRAM_BOT_TOKEN"))
                                _console.add(formatter.format(record), level)
                        else:
                            print(f"[{self.name}] {line}", end="", flush=True)
                    except (OSError, ValueError):
                        writable = False
        except (OSError, ValueError):
            pass

    def stop(self, graceful_seconds: float = 5.0) -> None:
        proc = self.process
        if getattr(self, "session", None) is not None:
            self.session.wait_process(proc, self.name)
            output(f"Зупинено {self.name}")
            return
        if proc.poll() is not None:
            return
        signo = signal.SIGINT if self.name == "radio" else signal.SIGTERM
        try:
            os.killpg(proc.pid, signo)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=graceful_seconds)
        except subprocess.TimeoutExpired:
            output(f"{self.name} не завершився; завершую примусово")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        output(f"Зупинено {self.name}")


def wait_for_api(api: ManagedProcess, deadline_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if api.process.poll() is not None:
            raise RuntimeError(
                service_failure(api, f"FastAPI завершився з кодом {api.process.returncode}; докладно в launcher.log")
            )
        if api_reachable():
            output("FastAPI відповідає на 127.0.0.1:8765 (401 без авторизації — норма)")
            return
        time.sleep(0.3)
    raise RuntimeError("FastAPI не відповідає на порту 8765; див. журнал [api]")


def wait_for_ngrok(tunnel: ManagedProcess, deadline_seconds: float = 15.0) -> str | None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if tunnel.process.poll() is not None:
            raise RuntimeError(
                service_failure(tunnel, f"ngrok завершився з кодом {tunnel.process.returncode}; докладно в launcher.log")
            )
        try:
            found = read_ngrok_https_url()
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            found = None
        if found:
            return found
        time.sleep(0.4)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Radio 345: запуск і коректна зупинка всіх служб"
    )
    parser.add_argument("--local-admin", action="store_true", help=argparse.SUPPRESS)  # сумісність: панель тепер типово увімкнена
    parser.add_argument("--no-local-admin", action="store_true", help="не запускати локальну адмінпанель на 127.0.0.1:8766")
    parser.add_argument(
        "--local-miniapp", action="store_true",
        help="додатково запустити локальний перегляд Mini App на 127.0.0.1:8080",
    )
    parser.add_argument(
        "--publish-pages", action="store_true",
        default=launcher_setting("RADIO345_AUTO_PUBLISH_PAGES") == "1",
        help="оновити Mini App та зробити commit/push у main (або RADIO345_AUTO_PUBLISH_PAGES=1 у .env)",
    )
    parser.add_argument(
        "--ngrok-url", default=launcher_setting("RADIO345_NGROK_URL"),
        help="постійний HTTPS-домен ngrok (або RADIO345_NGROK_URL в .env)",
    )
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--status", action="store_true", help="показати керований запуск, порти та блокування приймача")
    control.add_argument("--stop", action="store_true", help="зупинити лише власний керований запуск")
    control.add_argument("--restart", action="store_true",
                         help="перезапустити всі служби запущеного лаунчера в його ж вікні")
    control.add_argument("--receiver-stop", action="store_true",
                         help="зупинити лише приймач (звільнити пам’ять для прогонів); решта служб працює")
    control.add_argument("--receiver-start", action="store_true", help="знову запустити приймач у запущеному лаунчері")
    args = parser.parse_args()

    try:
        if args.status:
            show_status()
            return 0
        if args.stop:
            if request_stop(ROOT):
                output("Керований запуск зупинено")
                return 0
            show_status()
            output("Немає керованого запуску. Сторонні та старі некеровані процеси не завершувались")
            return 2
        if args.restart:
            if request_restart(ROOT):
                output("Запит перезапуску надіслано: лаунчер зупинить і знову запустить служби")
                return 0
            show_status()
            output("Немає керованого запуску, перезапускати нічого")
            return 2
        if args.receiver_stop or args.receiver_start:
            return receiver_command("receiver-stop" if args.receiver_stop else "receiver-start")
        return run_until_stopped(args)
    except KeyboardInterrupt:
        output("Завершення запускателя запитано")
        return 0
    except (OSError, RuntimeError) as exc:
        output(str(exc))
        return 1


RESTART_WAIT_SECONDS = 60


def receiver_command(command, timeout=RESTART_WAIT_SECONDS):
    """Зупинка чи запуск лише приймача; чекає, доки .radio345.lock звільниться / зайнятий."""
    if not send_command(ROOT, command):
        output("Немає керованого запуску лаунчера")
        return 2
    want_held = command == "receiver-start"
    deadline = time.monotonic() + timeout
    while lock_held(ROOT / ".radio345.lock") != want_held:
        if time.monotonic() >= deadline:
            output("Команду надіслано, але приймач ще не " + ("запустився" if want_held else "зупинився") + "; перевірте --status")
            return 1
        time.sleep(0.5)
    output("Приймач запущено" if want_held else "Приймач зупинено; панель, API й Mini App працюють")
    return 0


def open_session(names, *, retry_seconds=0.0):
    """Сесія лаунчера; після перезапуску чекає, доки старі контролери звільнять блокування."""
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return LauncherSession(ROOT, names, log=output).__enter__()
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def run_until_stopped(args):
    """Запуск служб; команда «перезапуск» (з панелі чи --restart) запускає їх знову."""
    restarting = False
    while True:
        # Одне рішення для складу сесії й для запуску: інакше сесія не знає служби «admin».
        args.start_admin = should_start_admin(args)
        names = ["api", "ngrok"] + (["admin"] if args.start_admin else []) + (["miniapp"] if args.local_miniapp else []) + ["radio"]
        session = open_session(names, retry_seconds=RESTART_WAIT_SECONDS if restarting else 0.0)
        try:
            if restarting:
                wait_receiver_released()
            code = run_services(args, session)
        finally:
            session.__exit__(None, None, None)
        if not session.restart_requested:
            return code
        output("Перезапуск: служби зупинено, запускаю знову…")
        restarting = True


def wait_receiver_released(timeout=RESTART_WAIT_SECONDS):
    """Попередній приймач звільняє .radio345.lock після черги Telegram."""
    deadline = time.monotonic() + timeout
    while lock_held(ROOT / ".radio345.lock") and time.monotonic() < deadline:
        time.sleep(0.5)


def show_status():
    state = managed_status(ROOT)
    output("Керований запуск: " + (state["state"] if state["managed"] else "немає"))
    if state["managed"]:
        output(f"PID запускателя: {state['launcher_pid']}; контролери: {state['controllers']}")
        if not state["launcher_reachable"]:
            output("Запускатель недоступний; контролери автоматично завершують служби")
    for port, name in ((API_PORT, "FastAPI"), (NGROK_INSPECT_PORT, "ngrok"), (8766, "Локальна панель")):
        output(f"{name}: TCP {port} — {'зайнятий' if port_busy(port) else 'вільний'}")
    output("Блокування приймача: " + ("утримується" if lock_held(ROOT / ".radio345.lock") else "вільне"))


def should_start_admin(args):
    """Панель некритична: якщо порт уже зайнятий окремою панеллю, просто користуємось нею."""
    if getattr(args, "no_local_admin", False):
        return False
    if port_busy(8766):
        output("Локальна панель уже працює окремо на 127.0.0.1:8766 — використовую її")
        return False
    return True


def run_services(args, session):

    if not (ROOT / "radio.py").is_file() or not (ROOT / "api/local_server.py").is_file():
        output("Помилка: покладіть start_radio345.py у корінь репозиторію поруч із radio.py")
        return 2
    if not (ROOT / ".env").is_file():
        output("Помилка: не знайдено .env (Telegram-токен має залишатися локальним)")
        return 2
    if lock_held(ROOT / ".radio345.lock"):
        output("Інший приймач уже працює; нові служби не запущено. Перевірте --status")
        return 2
    py = get_python()
    db_path = Path(config.DB_PATH)
    if not ensure_dictionaries(
        db_path, ROOT / "dictionaries.txt", py,
        interactive=interactive_console(),
        ask=lambda prompt: input(f"[launcher] {prompt}"),
        out=output,
    ):
        return 2
    if not ensure_admin(
        db_path,
        launcher_setting("TELEGRAM_BOT_TOKEN"),
        launcher_setting("TELEGRAM_CHAT_ID"),
        f"{py} -m api.bootstrap_admin",
        interactive=interactive_console(),
        ask=lambda prompt: input(f"[launcher] {prompt}"),
        out=output,
    ):
        return 2
    try:
        desired_ngrok_url = valid_https_endpoint(args.ngrok_url) if args.ngrok_url else None
    except ValueError as exc:
        output(str(exc))
        return 2
    ngrok_executable = shutil.which("ngrok")
    if not ngrok_executable:
        output("Помилка: ngrok не знайдено в PATH; перевірте встановлення ngrok")
        return 2

    ports = [(API_PORT, "FastAPI"), (NGROK_INSPECT_PORT, "ngrok")]
    start_admin = args.start_admin if hasattr(args, "start_admin") else should_start_admin(args)
    if args.local_miniapp:
        ports.append((MINIAPP_PORT, "Mini App preview"))
    for port, name in ports:
        if port_busy(port):
            output(f"Помилка: порт {port} зайнятий ({name}). Зупиніть раніше запущену службу.")
            return 2

    start_console()
    if not start_admin and port_busy(8766):
        _console.set_admin_url("http://127.0.0.1:8766")
    output(f"Python: {py}")
    processes: list[ManagedProcess] = []
    failure = None
    try:
        api = ManagedProcess("api", [py, "-u", "-m", "api.local_server"], session=session)
        processes.append(api)
        wait_for_api(api)

        ngrok_command = [ngrok_executable, "http", str(API_PORT)]
        if desired_ngrok_url:
            ngrok_command.extend(["--url", desired_ngrok_url])
        ngrok_command.append("--log=stdout")
        tunnel = ManagedProcess("ngrok", ngrok_command, session=session)
        processes.append(tunnel)
        output("Підключаю ngrok: очікую HTTPS-адресу (до 15 с)…")
        actual_url = wait_for_ngrok(tunnel)
        expected_url = read_expected_api_url()
        if not actual_url:
            raise RuntimeError(service_failure(tunnel, "ngrok не повернув HTTPS-адресу за відведений час. Перевірте інтернет / VPN. Докладно в launcher.log."))
        actual_url = valid_https_endpoint(actual_url)
        output(f"HTTPS API ngrok: {actual_url}")
        if desired_ngrok_url and actual_url != desired_ngrok_url:
            raise RuntimeError("ngrok відкрив іншу адресу замість зазначеного постійного домену")

        # A previous run may already have changed local HTML while leaving
        # GitHub Pages untouched. Publish that single pending URL edit too.
        pending_local_url = False
        if args.publish_pages:
            pending_local_url = "miniapp/index.html" in git("diff", "--name-only").splitlines()
            if expected_url != actual_url or pending_local_url:
                check_pages_publish_ready()

        if expected_url != actual_url:
            update_miniapp_api_url(actual_url)
        else:
            output("Адреса в локальному Mini App вже збігається з ngrok")

        if args.publish_pages and (expected_url != actual_url or pending_local_url):
            publish_miniapp_url()
            output("Перевіряю публікацію GitHub Pages...")
            if wait_for_pages_url(actual_url):
                output("✅ GitHub Pages використовує актуальну адресу ngrok")
            else:
                output("⚠️ Публікацію ще не підтверджено. Перевірте GitHub Actions")
        elif expected_url != actual_url:
            output("⚠️ Локальний URL оновлено, але GitHub Pages поки використовує старий URL!")
            output("  Перезапустіть з --publish-pages або використовуйте постійний домен ngrok")

        if args.local_miniapp:
            preview = ManagedProcess(
                "miniapp", [
                    py, "-u", "-m", "http.server", str(MINIAPP_PORT),
                    "--bind", "127.0.0.1", "--directory", str(ROOT / "miniapp"),
                ], session=session,
            )
            processes.append(preview)
            output("Локальний перегляд Mini App: http://127.0.0.1:8080")

        if start_admin:
            admin = ManagedProcess("admin", [py,"-u","-m","api.local_dashboard"], session=session)
            processes.append(admin)
            _console.set_admin_url("http://127.0.0.1:8766")
            output("Локальна панель: http://127.0.0.1:8766  (відкрити: .venv/bin/python -m api.local_dashboard --open)")
        _console.expect_receiver()
        radio = ManagedProcess("radio", [py, "-u", str(ROOT / "radio.py")], session=session)
        processes.append(radio)
        output("Усі запитані процеси запущено. Ctrl+C — зупинити їх разом.")
        while True:
            request = session.take_request()
            radio = next((item for item in processes if item.name == "radio"), None)
            if request == "receiver-stop" and radio is not None:
                output("Зупиняю лише приймач за запитом; панель, API й Mini App працюють далі")
                session.stop_service(radio.process, "radio")
                processes.remove(radio)
                output("Приймач зупинено. Запустити: --receiver-start або кнопка в панелі")
            elif request == "receiver-start" and radio is None:
                if lock_held(ROOT / ".radio345.lock"):
                    output("Приймач не запущено: блокування ще утримується іншим процесом")
                else:
                    if _console is not None:
                        _console.expect_receiver()
                    processes.append(ManagedProcess("radio", [py, "-u", str(ROOT / "radio.py")], session=session))
            for item in processes:
                exit_code = item.process.poll()
                if exit_code is not None and item.name == "admin":
                    # Падіння панелі не повинно зупиняти приймання ефіру.
                    output(f"⚠️ Локальна панель завершилась (код {exit_code}); приймач працює далі. "
                           "Запустити знову: .venv/bin/python -m api.local_dashboard --open")
                    processes.remove(item)
                    _console.set_admin_url(None)
                    break
                if exit_code is not None:
                    failure = safe_message(service_failure(item, f"{item.name} завершився з кодом {exit_code}; докладно в launcher.log / radio.log"))
                    if _console is not None:
                        _console.set_problem(failure)
                    output(f"Помилка: {failure}")
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        output("Отримано запит завершення (Ctrl+C/SIGTERM/SIGHUP). Зупиняю служби...")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        failure = safe_message(str(exc))
        if _console is not None:
            _console.set_problem(failure)
        output(f"Не вдалося запустити Radio 345: {failure}")
        return 1
    finally:
        session.begin_shutdown()
        # Сначала даём radio закрыть очередь Telegram, пока сеть ещё доступна.
        # Repeated Ctrl+C must not leave the remaining services running.
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for item in reversed(processes):
                try:
                    item.stop(graceful_seconds=30.0 if item.name == "radio" else 5.0)
                except OSError:
                    output(f"Не вдалося зупинити {item.name}; перевірте процес вручну")
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            close_console()
            if failure:
                # Print after leaving the alternate screen so the reason survives exit.
                output(f"Не вдалося запустити / продовжити Radio 345: {failure}")
                output("Подробиці: launcher.log та radio.log. Після усунення причини повторіть запуск.")


if __name__ == "__main__":
    raise SystemExit(main())
