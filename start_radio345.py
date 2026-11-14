#!/usr/bin/env python3
"""Запуск Radio 345: FastAPI, ngrok, приёмник и необязательный Mini App preview.

Положите этот файл в корень репозитория рядом с radio.py.

    python start_radio345.py
    python start_radio345.py --local-miniapp
    python start_radio345.py --publish-pages
    python start_radio345.py --ngrok-url https://YOUR_DEV_DOMAIN.ngrok-free.app

Ctrl+C останавливает только процессы, запущенные этим экземпляром.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
API_PORT = 8765
NGROK_INSPECT_PORT = 4040
MINIAPP_PORT = 8080


def output(message: str) -> None:
    print(f"[launcher] {message}", flush=True)


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
    local_python = ROOT / "venv" / "bin" / "python"
    return str(local_python) if local_python.is_file() else sys.executable


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
        raise ValueError("HTTPS URL должен быть строкой")
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
        raise ValueError("Некорректный HTTPS URL ngrok")
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
        raise RuntimeError("В miniapp/index.html должен быть ровно один meta radio345-api")
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
    output(f"Локальный Mini App API URL обновлён: {old_url} → {actual_url}")
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
            f"Команда git {args[0]} завершилась с кодом {run.returncode}; "
            "проверь git status и настройку доступа к origin вручную"
        )
    return run.stdout.strip()


def check_pages_publish_ready() -> None:
    if not shutil.which("git"):
        raise RuntimeError("git не найден в PATH")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
        raise RuntimeError("Запускатель должен находиться в корне git-репозитория")
    branch = git("branch", "--show-current")
    if branch != "main":
        raise RuntimeError(f"Автопубликация разрешена только из ветки main (сейчас {branch!r})")
    # Do not publish unrelated tracked/staged work. An earlier launcher run may
    # already have changed just the API URL in index.html; that is safe to reuse.
    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Есть staged-изменения; не публикую автоматически")
    changed_paths = git("diff", "--name-only").splitlines()
    if changed_paths and changed_paths != ["miniapp/index.html"]:
        raise RuntimeError("Есть изменения кроме Mini App URL; не публикую автоматически")
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
            raise RuntimeError("index.html изменён не только в URL; не публикую автоматически")
    git("fetch", "origin", "main", timeout=60)
    if git("rev-parse", "HEAD") != git("rev-parse", "refs/remotes/origin/main"):
        raise RuntimeError("Локальная ветка и origin/main различаются; синхронизируй их вручную")


def publish_miniapp_url() -> None:
    """Commit & push only the HTML file, then GitHub Pages workflow deploys it."""
    git("add", "--", "miniapp/index.html")
    # --only prevents accidental inclusion of other staged files.
    git("commit", "--only", "-m", "Radio 345: sync public API endpoint", "--", "miniapp/index.html")
    git("push", "origin", "main", timeout=90)
    output("Новый URL отправлен в GitHub. GitHub Actions опубликует обновление Mini App")


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
    def __init__(self, name: str, command: list[str]):
        self.name = name
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,  # отдельная группа: Ctrl+C не убьёт детей внезапно
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        output(f"Запущен {name} (PID {self.process.pid})")

    def _read_output(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            print(f"[{self.name}] {line}", end="", flush=True)

    def stop(self, graceful_seconds: float = 5.0) -> None:
        proc = self.process
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
            output(f"{self.name} не завершился; завершаю принудительно")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        output(f"Остановлен {self.name}")


def wait_for_api(api: ManagedProcess, deadline_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if api.process.poll() is not None:
            raise RuntimeError(
                f"FastAPI завершился с кодом {api.process.returncode}; см. лог [api]"
            )
        if api_reachable():
            output("FastAPI отвечает на 127.0.0.1:8765 (401 без авторизации — норма)")
            return
        time.sleep(0.3)
    raise RuntimeError("FastAPI не отвечает на порту 8765; см. лог [api]")


def wait_for_ngrok(tunnel: ManagedProcess, deadline_seconds: float = 15.0) -> str | None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if tunnel.process.poll() is not None:
            raise RuntimeError(
                f"ngrok завершился с кодом {tunnel.process.returncode}; см. лог [ngrok]"
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
        description="Radio 345: запуск и корректная остановка всех сервисов"
    )
    parser.add_argument(
        "--local-miniapp", action="store_true",
        help="дополнительно запустить локальный просмотр Mini App на 127.0.0.1:8080",
    )
    parser.add_argument(
        "--publish-pages", action="store_true",
        default=launcher_setting("RADIO345_AUTO_PUBLISH_PAGES") == "1",
        help="обновить Mini App и сделать commit/push в main (или RADIO345_AUTO_PUBLISH_PAGES=1 в .env)",
    )
    parser.add_argument(
        "--ngrok-url", default=launcher_setting("RADIO345_NGROK_URL"),
        help="постоянный HTTPS-домен ngrok (или RADIO345_NGROK_URL в .env)",
    )
    args = parser.parse_args()

    if not (ROOT / "radio.py").is_file() or not (ROOT / "api/local_server.py").is_file():
        output("Ошибка: положи start_radio345.py в корень репозитория рядом с radio.py")
        return 2
    if not (ROOT / ".env").is_file():
        output("Ошибка: не найден .env (Telegram-токен должен оставаться локальным)")
        return 2
    if not (ROOT / "events.db").is_file():
        output("Ошибка: не найдена рабочая events.db; не создаю пустую базу")
        return 2
    try:
        desired_ngrok_url = valid_https_endpoint(args.ngrok_url) if args.ngrok_url else None
    except ValueError as exc:
        output(str(exc))
        return 2
    ngrok_executable = shutil.which("ngrok")
    if not ngrok_executable:
        output("Ошибка: ngrok не найден в PATH; проверь установку ngrok")
        return 2

    ports = [(API_PORT, "FastAPI"), (NGROK_INSPECT_PORT, "ngrok")]
    if args.local_miniapp:
        ports.append((MINIAPP_PORT, "Mini App preview"))
    for port, name in ports:
        if port_busy(port):
            output(f"Ошибка: порт {port} занят ({name}). Останови ранее запущенный сервис.")
            return 2

    py = get_python()
    output(f"Python: {py}")
    processes: list[ManagedProcess] = []
    try:
        api = ManagedProcess("api", [py, "-u", "-m", "api.local_server"])
        processes.append(api)
        wait_for_api(api)

        ngrok_command = [ngrok_executable, "http", str(API_PORT)]
        if desired_ngrok_url:
            ngrok_command.extend(["--url", desired_ngrok_url])
        ngrok_command.append("--log=stdout")
        tunnel = ManagedProcess("ngrok", ngrok_command)
        processes.append(tunnel)
        actual_url = wait_for_ngrok(tunnel)
        expected_url = read_expected_api_url()
        if not actual_url:
            raise RuntimeError("ngrok не отдал HTTPS URL. Останавливаю запуск, чтобы не использовать неверный адрес")
        actual_url = valid_https_endpoint(actual_url)
        output(f"HTTPS API ngrok: {actual_url}")
        if desired_ngrok_url and actual_url != desired_ngrok_url:
            raise RuntimeError("ngrok открыл другой адрес вместо указанного постоянного домена")

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
            output("Адрес в локальном Mini App уже совпадает с ngrok")

        if args.publish_pages and (expected_url != actual_url or pending_local_url):
            publish_miniapp_url()
            output("Проверяю публикацию GitHub Pages...")
            if wait_for_pages_url(actual_url):
                output("✅ GitHub Pages использует актуальный адрес ngrok")
            else:
                output("⚠️ Публикация ещё не подтверждена. Посмотри GitHub Actions")
        elif expected_url != actual_url:
            output("⚠️ Локальный URL обновлён, но GitHub Pages пока использует старый URL!")
            output("  Перезапусти с --publish-pages или используй постоянный ngrok-домен")

        if args.local_miniapp:
            preview = ManagedProcess(
                "miniapp", [
                    py, "-u", "-m", "http.server", str(MINIAPP_PORT),
                    "--bind", "127.0.0.1", "--directory", str(ROOT / "miniapp"),
                ]
            )
            processes.append(preview)
            output("Локальный просмотр Mini App: http://127.0.0.1:8080")

        radio = ManagedProcess("radio", [py, "-u", str(ROOT / "radio.py")])
        processes.append(radio)
        output("Все запрошенные процессы запущены. Ctrl+C — остановить их вместе.")
        while True:
            for item in processes:
                exit_code = item.process.poll()
                if exit_code is not None:
                    output(f"⚠️ {item.name} завершился с кодом {exit_code}; останавливаю остальные")
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        output("Получен Ctrl+C. Останавливаю сервисы...")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        output(f"Не удалось запустить Radio 345: {exc}")
        return 1
    finally:
        # Сначала даём radio закрыть очередь Telegram, пока сеть ещё доступна.
        # Repeated Ctrl+C must not leave the remaining services running.
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for item in reversed(processes):
                try:
                    item.stop(graceful_seconds=18.0 if item.name == "radio" else 5.0)
                except OSError:
                    output(f"Не удалось остановить {item.name}; проверь процесс вручную")
        finally:
            signal.signal(signal.SIGINT, previous_handler)
        output("Остановка завершена")


if __name__ == "__main__":
    raise SystemExit(main())
