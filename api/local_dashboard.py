"""Окрема локальна адмінпанель. Ніколи не монтувати на публічному API/ngrok."""

import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

import config
from api.app import create_app
from api.management import SettingsUpdate
from services import miniapp_management as store
from services.notification_samples import add_sample, delete_sample, list_samples, MAX_UPLOAD_BYTES, validate_wav
from services.receiver_status import read_receiver_status
from services.sqlite_connection import get_db
from services.user_store import PERMISSIONS
from services.telegram_sync import sync_status

PORT = 8766
ROOT = Path(__file__).resolve().parents[1]


class SessionInput(BaseModel):
    token: str = Field(max_length=128)


def read_diagnostics(path):
    path = Path(path)
    if not path.is_file():
        return {"available":False}
    if path.stat().st_size > 2_000_000:
        raise ValueError("Файл діагностики завеликий")
    data = json.loads(path.read_text())
    # Telemetry only; ignore any future unrelated root fields and arbitrary strings.
    def numbers(value):
        if value is None or type(value) is bool: return value
        if type(value) in (int,float): return value if math.isfinite(value) else None
        if isinstance(value,list): return [numbers(x) for x in value[:128]]
        if isinstance(value,dict): return {k:numbers(v) for k,v in value.items() if isinstance(k,str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,90}",k)}
        if value in ("ready","ready_fallback","notification","low_rms","empty_text","garbage","blacklist","stale","error","whisper_error","discarded","adc","callback","s","ratio"): return value
        return None
    return {"available":True, **{k:numbers(data[k]) for k in ("schema","started_at","updated_at","counters","gauges","timings","asr_latency") if k in data}}


def create_dashboard(db_path, capability, *, port=PORT, diagnostics_path=None, samples_dir=None, bot_token="local-preview"):
    if len(capability)<32:
        raise ValueError("Надто короткий локальний ключ")
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    samples_dir = Path(samples_dir or config.NOTIFICATION_SOUNDS_DIR)
    diagnostics_path = Path(diagnostics_path or config.BASE_DIR/"diagnostics.json")
    host = f"127.0.0.1:{port}"
    origin = "http://"+host
    cookie = "radio345_local_admin"
    session = secrets.token_urlsafe(32)

    def matches(value, expected):
        return isinstance(value,str) and value.isascii() and secrets.compare_digest(value,expected)

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.headers.get("host") != host or request.client is None or request.client.host not in ("127.0.0.1","::1"):
            return Response(status_code=403)
        if request.method not in ("GET","HEAD") and request.headers.get("origin") != origin:
            return Response(status_code=403)
        public = request.url.path in ("/","/admin.js","/styles.css","/session")
        if not public and not matches(request.cookies.get(cookie,""),session):
            return Response(status_code=401)
        try:
            response = await call_next(request)
        except (sqlite3.Error,OSError,ValueError):
            response = Response("Локальні дані тимчасово недоступні",status_code=503)
        response.headers.update({"Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
            "X-Content-Type-Options":"nosniff",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-src 'self'; frame-ancestors 'self'"})
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
        response.set_cookie(cookie,session,httponly=True,samesite="strict",max_age=43200,path="/")
        return response

    @app.get("/admin.js")
    def script(): return Response((ROOT/"local_admin/admin.js").read_text(),media_type="text/javascript")

    @app.get("/styles.css")
    def styles(): return Response((ROOT/"local_admin/styles.css").read_text(),media_type="text/css")

    @app.get("/api/summary")
    def summary(admin=Depends(actor)):
        return {"receiver":read_receiver_status(db_path),"overview":store.overview(db_path),
            "settings":store.read_settings(db_path),"roles":{k:sorted(v) for k,v in PERMISSIONS.items()},
            "notification_filter":bool(config.FILTER_NOTIFICATION_SOUNDS),
            "telegram_sync":sync_status(db_path)}

    @app.get("/api/diagnostics")
    def diagnostics(admin=Depends(actor)): return read_diagnostics(diagnostics_path)

    @app.put("/api/settings")
    def settings(payload: SettingsUpdate, admin=Depends(actor)):
        try:
            return store.write_settings(db_path,admin,payload.values.model_dump(),payload.expected_revision)
        except store.ManagementConflict as exc:
            raise HTTPException(409,str(exc)) from None

    @app.get("/api/samples")
    def samples(admin=Depends(actor)): return list_samples(samples_dir)

    async def upload(request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>MAX_UPLOAD_BYTES:
                raise HTTPException(413,"Зразок завеликий")
        return bytes(content)

    @app.put("/api/samples")
    async def sample_upload(request: Request, admin=Depends(actor)):
        content = await upload(request)
        try:
            return add_sample(samples_dir,content,confirmed_isolated=request.headers.get("x-confirm-isolated")=="yes")
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.post("/api/samples/test")
    async def sample_test(request: Request, admin=Depends(actor)):
        import io
        import logging
        import wave
        from core.notification_filter import NotificationSoundFilter
        content = await upload(request)
        try:
            content,_ = validate_wav(content)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        with wave.open(io.BytesIO(content),"rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        detector = NotificationSoundFilter.from_directory(samples_dir,16000,logging.getLogger(__name__))
        return {"isolated_match":detector.is_notification(pcm),"templates":len(detector.templates)}

    @app.delete("/api/samples/{sample_id}")
    def sample_delete(sample_id: str,admin=Depends(actor)):
        try:
            return delete_sample(samples_dir, sample_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

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


def main():
    from dotenv import load_dotenv
    load_dotenv(config.BASE_DIR/".env")
    if not Path(config.DB_PATH).is_file():
        raise SystemExit("Робочу базу не знайдено")
    directory = config.BASE_DIR/".radio345-local-admin"
    directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink():
        raise SystemExit("Каталог локального доступу не може бути посиланням")
    directory.chmod(0o700)
    capability = secrets.token_urlsafe(32)
    # Capability file contains the URL; never put it in launcher/shared logs.
    path = directory/"access-url.txt"
    fd = os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w") as output:
        output.write(f"http://127.0.0.1:{PORT}/#"+capability+"\n")
    print(f"Локальна панель: http://127.0.0.1:{PORT}/")
    print("Приватне посилання входу: .radio345-local-admin/access-url.txt (відкрити локально)")
    app = create_dashboard(config.DB_PATH,capability,bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "local-preview"))
    uvicorn.run(app,host="127.0.0.1",port=PORT,access_log=False,proxy_headers=False)


if __name__ == "__main__":
    main()
