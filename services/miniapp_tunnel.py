"""Публічна HTTPS-адреса API для Mini App: ngrok з постійним доменом, окремий процес.

Нічого не комітить, не пушить і не змінює miniapp/: якщо адреса в сторінці інша — лише попереджає.
Запуск: ``./r345 up --only api tunnel`` (потрібні ngrok і RADIO345_NGROK_URL у .env).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
API_PORT = 8765
INSPECT_PORT = 4040
START_S = 20.0


def valid_https_endpoint(url):
    """Лише https://домен — без порту, шляху, користувача й параметрів."""
    if not isinstance(url, str):
        raise ValueError("HTTPS URL має бути рядком")
    parsed = urlsplit(url.strip())
    labels = (parsed.hostname or "").split(".")
    if (parsed.scheme != "https" or len(labels) < 2
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or parsed.username or parsed.password or parsed.port is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or len(url) > 255):
        raise ValueError("Некоректний HTTPS URL ngrok")
    return f"https://{parsed.hostname.lower()}"


def page_api_url(root=ROOT):
    """Адреса API, записана в miniapp/index.html, або None."""
    try:
        html = (Path(root) / "miniapp" / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'<meta\s+name=["\']radio345-api["\']\s+content=["\']([^"\']+)["\']', html)
    return match.group(1).rstrip("/") if match else None


def api_reachable(port=API_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.0) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return exc.code in (401, 403)   # без initData — норма
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def public_urls(port=INSPECT_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tunnels", timeout=1.0) as response:
            doc = json.load(response)
        return [t["public_url"].rstrip("/") for t in doc.get("tunnels", [])
                if str(t.get("public_url", "")).startswith("https://")]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def important(line):
    """Лише попередження й помилки ngrok: у журналі звичайних запитів бувають адреси відвідувачів."""
    return re.search(r"\blvl=(warn|eror|crit)\b|^ERROR", line) is not None


def run(desired, executable, stop, say, *, reachable=api_reachable, urls=public_urls, page=page_api_url,
        sleep=None, start_s=START_S):
    """Код завершення: 0 — штатна зупинка, інакше r345 перезапустить із паузою."""
    wait = sleep or stop.wait
    reported = False
    while not reachable():
        if not reported:
            say("Чекаю API на 127.0.0.1:8765")
            reported = True
        if wait(1.0) or stop.is_set():
            return 0
    if page() != desired:
        say("⚠️ Адреса в miniapp/index.html інша: опублікована Mini App не побачить цей API. Файл не змінено.")
    process = subprocess.Popen([executable, "http", str(API_PORT), "--url", desired, "--log=stdout", "--log-level=warn"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors="replace")

    def pump():
        for line in process.stdout:
            if important(line):
                say("ngrok: " + line.strip()[:300])

    threading.Thread(target=pump, daemon=True).start()
    try:
        deadline, opened = time.monotonic() + start_s, False
        while not stop.is_set():
            if process.poll() is not None:
                say(f"ngrok завершився (код {process.returncode})")
                return 1
            if not opened:
                found = urls()
                if desired in found:
                    opened = True
                    say(f"Публічна адреса API: {desired}")
                elif found:
                    say("ngrok відкрив іншу адресу замість постійного домену; зупиняю")
                    return 1
                elif time.monotonic() > deadline:
                    say("ngrok не підтвердив адресу: перевірте інтернет і авторизацію ngrok")
                    return 1
            wait(0.5)
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    try:
        desired = valid_https_endpoint(os.environ.get("RADIO345_NGROK_URL", "").strip())
    except ValueError:
        raise SystemExit("RADIO345_NGROK_URL у .env має бути https://постійний-домен") from None
    executable = shutil.which("ngrok")
    if not executable:
        raise SystemExit("Не знайдено ngrok")
    if public_urls():
        raise SystemExit("ngrok уже працює на цьому пристрої")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: stop.set())
    raise SystemExit(run(desired, executable, stop, lambda text: print(text, flush=True)))


if __name__ == "__main__":
    sys.exit(main())
