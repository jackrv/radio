"""Окрема локальна адмінпанель. Ніколи не монтувати на публічному API/ngrok."""
import argparse
import logging
import os
import socket
import threading
import time
import webbrowser
from pathlib import Path
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
import uvicorn

import config
from api.app import create_app
from services.sqlite_connection import get_db
from services.user_store import PERMISSIONS
from core.diagnostic_history import DiagnosticsHistory, history_directory
from api.panel import jobs, lab, overview, people, recognition, signals, system, mentions
from api.panel import recordings as recordings_api
from api.panel.common import session_value
from api.panel.models import SessionInput

PORT = 8766
ROOT = Path(__file__).resolve().parents[1]
ACCESS_DIR = config.BASE_DIR / ".radio345-local-admin"
SESSION_DAYS = 30
PANEL_JS = ROOT / "local_admin/js"


def panel_script():
    """Скрипт панелі: розділи з local_admin/js/ за номерами, в одній функції (як раніше один файл).

    Збірки немає: файли склеюються при кожному запиті, тож правка діє після оновлення сторінки.
    """
    parts = "".join(p.read_text() for p in sorted(PANEL_JS.glob("*.js")))
    return ('"use strict";\n// Radio 345 · локальна панель. Лише 127.0.0.1; дані з того самого Mac.\n'
            "(() => {\n" + parts + "})();\n")


def create_dashboard(db_path, capability, *, port=PORT, diagnostics_path=None, samples_dir=None, bot_token="local-preview", backup_dir=None, recordings_dir=None, launcher_root=None, experiments_dir=None, suggestions=False, hosts=None, client_ok=None):
    if len(capability)<32:
        raise ValueError("Надто короткий локальний ключ")
    suggestion_worker = None
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if suggestion_worker:
                suggestion_worker.stop()
                suggestion_worker.thread.join(timeout=5)
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None, lifespan=lifespan)
    samples_dir = Path(samples_dir or config.NOTIFICATION_SOUNDS_DIR)
    diagnostics_path = Path(diagnostics_path or config.BASE_DIR/"diagnostics.json")
    diagnostic_history = DiagnosticsHistory(
        history_directory(diagnostics_path), retention_days=config.DIAGNOSTICS_HISTORY_DAYS,
        max_sessions=config.DIAGNOSTICS_HISTORY_MAX_SESSIONS,
        max_bytes=config.DIAGNOSTICS_HISTORY_MAX_BYTES,
    )
    from services.recordings import RecordingStore
    experiments_dir = Path(experiments_dir) if experiments_dir else ROOT / ".local-experiments"
    recordings = RecordingStore(recordings_dir or Path(db_path).resolve().parent / ".local-recordings",
                                retention_days=config.RECORDINGS_RETENTION_DAYS,
                                max_bytes=config.RECORDINGS_MAX_BYTES,
                                low_rms_retention_seconds=config.RECORDINGS_LOW_RMS_RETENTION_SECONDS)
    suggestion_worker = None
    if suggestions:
        # Фонова перевірка списків у процесі панелі: приймач не навантажується.
        from services.suggestions import SuggestionWorker
        suggestion_worker = SuggestionWorker(db_path, recordings, logging.getLogger("radio.suggestions")).start()
    # Збережені правки, після яких пропозиції перевіряються знову (перевірки тексту й пауза — ні).
    # Збережені правки панелі — у «Активність» (розмітка записів — ні: її сотні на день).
    panel_actions = (
        (re.compile(r"/api/suggestions/sync-events"), "Запит на оновлення подій за еталонами"),
        (re.compile(r"/api/suggestions/(dismiss|restore)"), "Пропозиції: приховано / повернуто"),
        (re.compile(r"/api/suggestions/mute"), "Пропозиції: перевірку виду вимкнено / увімкнено"),
        (re.compile(r"/api/audio-devices"), "Звукові пристрої приймача змінено"),
        (re.compile(r"/api/text-lists/([a-z_]+)"), "Фільтри тексту: {0}"),
        (re.compile(r"/api/dictionaries/([a-z_]+)"), "Словник: {0}"),
        (re.compile(r"/api/learning/[^/]+(/approval)?"), "Навчання: правило"),
        (re.compile(r"/api/users/([0-9]+)"), "Користувач {0}"),
        (re.compile(r"/api/(runtime-settings|settings)"), "Налаштування розпізнавання"),
        (re.compile(r"/api/daily-codes/(.+)"), "Коди доби: {0}"),
        (re.compile(r"/api/samples(/(?!test$)[^/]+)?|/api/recordings/[^/]+/notification-sample"), "Зразки сигналів"),
        (re.compile(r"/api/backups"), "Запит на створення копії бази"),
        (re.compile(r"/api/training/export"), "Запит на вивантаження набору для дообучення"),
        (re.compile(r"/api/receiver-pause"), "Пауза приймача"),
        (re.compile(r"/api/launcher/([a-z-]+)"), "Лаунчер: {0}"),
    )

    def log_panel_action(path, payload=None):
        for pattern, label in panel_actions:
            match = pattern.fullmatch(path)
            if match:
                from services import user_activity
                try:
                    admin = actor()
                except HTTPException:
                    return
                user_activity.record(db_path, admin, "panel", label.format(*[g or "" for g in match.groups()]),
                                     user_activity.panel_details(payload))
                return

    recheck_after = re.compile(r"/api/(recordings/[^/]+/review|text-lists/[a-z_]+|dictionaries/[a-z_]+|learning/.+|daily-codes/.+)")
    # Копії лежать поруч із самою базою: для робочої — .local-backups проєкту.
    backup_dir = Path(backup_dir) if backup_dir else Path(db_path).resolve().parent / ".local-backups"
    # hosts/client_ok задає лише режим Tailscale; типово — тільки цей комп'ютер.
    allowed_hosts = {f"{name}:{port}" for name in (hosts or ("127.0.0.1",))}
    allowed_origins = {"http://"+value for value in allowed_hosts}
    client_ok = client_ok or (lambda ip: ip in ("127.0.0.1", "::1"))
    cookie = "radio345_local_admin"
    session = session_value(capability)

    def matches(value, expected):
        return isinstance(value,str) and value.isascii() and secrets.compare_digest(value,expected)

    @app.middleware("http")
    async def local_only(request, call_next):
        started = time.perf_counter()
        if request.headers.get("host") not in allowed_hosts or request.client is None:
            return Response(status_code=403)
        if not await run_in_threadpool(client_ok, request.client.host):
            return Response(status_code=403)
        if request.method not in ("GET","HEAD") and request.headers.get("origin") not in allowed_origins:
            return Response(status_code=403)
        public = request.url.path in ("/","/admin.js","/styles.css","/session")
        if not public and not matches(request.cookies.get(cookie,""),session):
            return Response(status_code=401)
        activity_payload=None
        if request.method not in ('GET','HEAD') and any(p.fullmatch(request.url.path) for p,_ in panel_actions):
            length=request.headers.get('content-length','0')
            if length.isdigit() and 0<int(length)<=100_000:
                try: activity_payload=await request.json()
                except ValueError: pass
        try:
            response = await call_next(request)
        except (sqlite3.Error,OSError,ValueError):
            response = Response("Локальні дані тимчасово недоступні",status_code=503)
        if request.method not in ("GET","HEAD") and response.status_code < 400:
            if suggestion_worker and recheck_after.fullmatch(request.url.path):
                suggestion_worker.poke()
            if not request.url.path.endswith("/check") and not request.url.path.endswith("/evaluate"):
                log_panel_action(request.url.path, activity_payload)
        response.headers.update({"Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
            "X-Content-Type-Options":"nosniff",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-src 'self'; frame-ancestors 'self'"})
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        return response

    def actor():
        with get_db(db_path) as db:
            row = db.execute("SELECT user_id FROM telegram_users WHERE role='admin' AND enabled=1 ORDER BY user_id LIMIT 1").fetchone()
        if not row:
            raise HTTPException(403,"Немає активного адміністратора")
        return row[0]

    @app.get("/")
    def home():
        return HTMLResponse((ROOT/"local_admin/index.html").read_text())

    @app.post("/session")
    def login(payload: SessionInput):
        if not matches(payload.token,capability):
            raise HTTPException(403,"Недійсний локальний ключ")
        response = Response(status_code=204)
        response.set_cookie(cookie,session,httponly=True,samesite="strict",max_age=SESSION_DAYS*86400,path="/")
        return response

    @app.get("/admin.js")
    def script(): return Response(panel_script(),media_type="text/javascript")

    @app.get("/styles.css")
    def styles(): return Response((ROOT/"local_admin/styles.css").read_text(),media_type="text/css")

    # HTTP-адаптери розділів незалежні; спільна доменна логіка — у services/.
    ctx = SimpleNamespace(db_path=db_path, recordings=recordings, actor=actor, samples_dir=samples_dir, diagnostics_path=diagnostics_path, diagnostic_history=diagnostic_history, experiments_dir=experiments_dir, suggestion_worker=suggestion_worker, backup_dir=backup_dir, bot_token=bot_token, launcher_root=launcher_root, port=port)
    for section in (recordings_api, overview, recognition, signals, people, system, lab, jobs, mentions):
        section.register(app, ctx)

    @app.get("/preview-bridge.js")
    def bridge():
        return Response('window.Telegram={WebApp:{initData:"local-preview",ready(){},expand(){},close(){},onEvent(){},offEvent(){}}};',media_type="text/javascript")

    for role in PERMISSIONS:
        preview = create_app(db_path,bot_token)
        def make_identity(selected):
            def identity():
                return {"user_id":actor(),"role":selected,"_preview":True}
            return identity
        preview.dependency_overrides[preview.state.current_user] = make_identity(role)

        @preview.middleware("http")
        async def read_only(request,call_next):
            if request.method not in ("GET","HEAD"):
                return Response("Перегляд ролі: зміни заборонено",status_code=403)
            return await call_next(request)

        def make_home(selected):
            def preview_home():
                html = (ROOT / "miniapp/index.html").read_text()
                html = re.sub(r'(<meta name="radio345-api" content=")[^"]*',
                              r'\g<1>/preview/' + selected, html)
                html = html.replace('https://telegram.org/js/telegram-web-app.js', '/preview-bridge.js')
                return HTMLResponse(html)
            return preview_home
        preview.add_api_route("/",make_home(role),methods=["GET"])
        preview.mount("/",StaticFiles(directory=ROOT/"miniapp"))
        app.mount("/preview/"+role,preview)
    return app


def _private_dir():
    ACCESS_DIR.mkdir(mode=0o700, exist_ok=True)
    if ACCESS_DIR.is_symlink():
        raise SystemExit("Каталог локального доступу не може бути посиланням")
    ACCESS_DIR.chmod(0o700)


def _write_private(path, text):
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)


def load_capability(rotate=False):
    """Постійний локальний ключ (0600). Нове значення — лише за явним запитом."""
    _private_dir()
    key_path = ACCESS_DIR / "key"
    capability = None
    if not rotate and key_path.is_file() and not key_path.is_symlink():
        capability = key_path.read_text().strip()
    if not capability or len(capability) < 32:
        capability = secrets.token_urlsafe(32)
        _write_private(key_path, capability + "\n")
    _write_private(ACCESS_DIR / "access-url.txt", access_url(capability) + "\n")
    return capability


def access_url(capability):
    return f"http://127.0.0.1:{PORT}/#{capability}"


def tailnet_access():
    """RADIO345_PANEL_HOST=tailscale: адреса, дозволені імена й перевірка «пристрій власника»."""
    from services.radio_stream import TailnetPeers, _tailscale_status, client_allowed, tailscale_ipv4
    address = tailscale_ipv4()
    if not address:
        raise SystemExit("Tailscale не активний: панель не запущено")
    name = ((_tailscale_status().get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    hosts = [address, *([name, name.split(".")[0]] if name else [])]
    peers = TailnetPeers()
    return hosts, lambda ip: client_allowed(ip) and peers.is_owner(ip)


def port_busy(port=PORT):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def open_when_ready(url, timeout=20.0):
    def wait():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_busy():
                webbrowser.open(url)
                return
            time.sleep(0.3)
    threading.Thread(target=wait, daemon=True).start()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальна адмінпанель Radio 345 (лише 127.0.0.1)")
    parser.add_argument("--open", action="store_true", help="відкрити панель у браузері; якщо вона вже працює — лише відкрити")
    parser.add_argument("--rotate-key", action="store_true", help="створити новий ключ доступу (старі закладки перестануть діяти)")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(config.BASE_DIR/".env")
    if not Path(config.DB_PATH).is_file():
        raise SystemExit("Робочу базу не знайдено")
    hosts = client_ok = None
    if os.environ.get("RADIO345_PANEL_HOST", "").strip().lower() == "tailscale":
        hosts, client_ok = tailnet_access()
    capability = load_capability(rotate=args.rotate_key)
    url = access_url(capability)
    if hosts:
        url = url.replace("127.0.0.1", hosts[-1], 1)
        _write_private(ACCESS_DIR / "access-url.txt", url + "\n")

    if port_busy():
        # Панель уже працює (наприклад, її запустив start_radio345.py): лише відкриваємо.
        if args.open:
            webbrowser.open(url)
            print(f"Локальну панель відкрито: http://127.0.0.1:{PORT}/")
        else:
            print(f"Локальна панель уже працює: http://127.0.0.1:{PORT}/  (відкрити: python -m api.local_dashboard --open)")
        if args.rotate_key:
            print("Новий ключ діятиме після перезапуску панелі.")
        return 0

    print(f"Локальна панель: http://127.0.0.1:{PORT}/")
    print("Відкрити: .venv/bin/python -m api.local_dashboard --open")
    if args.open:
        open_when_ready(url)
    app = create_dashboard(config.DB_PATH,capability,recordings_dir=config.RECORDINGS_DIR,bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "local-preview"),suggestions=True,hosts=hosts,client_ok=client_ok)
    uvicorn.run(app,host=hosts[0] if hosts else "127.0.0.1",port=PORT,access_log=False,proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
