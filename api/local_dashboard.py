"""Окрема локальна адмінпанель. Ніколи не монтувати на публічному API/ngrok."""

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
import re
import secrets
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, Field, model_validator
import uvicorn

import config
from api.app import create_app
from services import miniapp_management as store
from services.notification_samples import add_sample, delete_sample, list_samples, MAX_UPLOAD_BYTES, validate_wav
from services import notification_eval
from services.receiver_status import read_receiver_status
from services.sqlite_connection import get_db
from services.user_store import PERMISSIONS
from services.telegram_sync import sync_status
from services.db_backup import auto_backups, create_backup
from services.diagnostic_history import DiagnosticsHistory, history_directory
from services.user_notify_store import effective_mode

PORT = 8766
ROOT = Path(__file__).resolve().parents[1]
ACCESS_DIR = config.BASE_DIR / ".radio345-local-admin"
SESSION_DAYS = 30


class RecordingReview(BaseModel):
    reference_recipient: str | None = Field(default=None, max_length=300)
    reference_sender: str | None = Field(default=None, max_length=300)
    reference: str = Field(max_length=2000)
    label: str = Field(pattern=r"^(speech|noise|notification|unclear|room)$")
    expected_revision: int = Field(ge=0)
    multiple_messages: bool | None = None


class SessionInput(BaseModel):
    token: str = Field(max_length=128)


class RoleSuggestion(BaseModel):
    text: str = Field(max_length=2000)


class TextCheck(BaseModel):
    text: str = Field(max_length=5000)
    at: float | None = Field(default=None, ge=0, le=4e9)   # час запису: коди доби цього дня


class DailyCodesUpdate(BaseModel):
    codes: str = Field(max_length=500)


class ListeningHold(BaseModel):
    seconds: float = Field(ge=0, le=120)


class RuntimeSettingsUpdate(BaseModel):
    values: dict[str, bool | int | float]
    expected_revision: int = Field(ge=0)


class TextListEntry(BaseModel):
    raw_line: str = Field(max_length=300)
    enabled: bool = Field(strict=True)


class TextListCheck(BaseModel):
    entries: list[TextListEntry] = Field(max_length=1000)


class TextListUpdate(BaseModel):
    entries: list[TextListEntry] = Field(max_length=500)
    expected_revision: int = Field(ge=0)


class DictionaryEntry(BaseModel):
    raw_line: str = Field(max_length=500)
    enabled: bool = Field(strict=True)


class DictionaryUpdate(BaseModel):
    entries: list[TextListEntry] = Field(max_length=1000)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleApproval(BaseModel):
    approved: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleUpdate(BaseModel):
    disabled: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class UserUpdate(BaseModel):
    role: Literal["admin", "editor", "viewer"]
    enabled: bool = Field(strict=True)
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    display_name: str | None = Field(default=None, max_length=80)
    notify_mode: Literal["all", "watch", "commands", "off"] | None = None


class RecognitionValues(BaseModel):
    USE_LLM: bool = Field(strict=True)
    VAD_LEVEL: int = Field(ge=0, le=3, strict=True)
    SILENCE_END_MS: int = Field(ge=150, le=3000, strict=True)
    MIN_SPEECH_MS: int = Field(ge=90, le=3000, strict=True)
    MAX_SEGMENT_S: int = Field(ge=3, le=60, strict=True)

    @model_validator(mode="after")
    def valid_duration(self):
        if self.MIN_SPEECH_MS >= self.MAX_SEGMENT_S * 1000:
            raise ValueError("Мінімальна тривалість мовлення має бути меншою за максимальний сегмент")
        return self


class SettingsUpdate(BaseModel):
    values: RecognitionValues
    expected_revision: int = Field(ge=0, strict=True)


class PauseUpdate(BaseModel):
    minutes: int = Field(ge=0, le=240, strict=True)
    expected_revision: int = Field(ge=0, strict=True)


class SampleConfirmation(BaseModel):
    confirmed_isolated: bool = Field(strict=True)


def spectral_settings():
    return {"mode": getattr(config, "SPECTRAL_NOTIFICATION_MODE", "off"),
            "threshold": float(getattr(config, "SPECTRAL_NOTIFICATION_THRESHOLD", 0.85)),
            "max_outside_s": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S", 0.2)),
            "max_extra": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_EXTRA", 0.02))}


def session_value(capability):
    """Сесія виводиться з постійного ключа: закладка працює й після перезапуску панелі."""
    return hmac.new(capability.encode(), b"radio345-local-session", hashlib.sha256).hexdigest()


def events_by_hour(db_path, hours=24, now=None):
    """Кількість подій за кожну з останніх ``hours`` годин (найстаріша — перша)."""
    now = time.time() if now is None else now
    start = int(now // 3600 - hours + 1) * 3600
    counts = [0] * hours
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT CAST(strftime('%s', ts) AS INTEGER) FROM radio_events WHERE ts >= datetime(?, 'unixepoch')",
            (start,),
        ).fetchall()
    for (stamp,) in rows:
        if stamp is not None and stamp >= start:
            index = int((stamp - start) // 3600)
            if 0 <= index < hours:
                counts[index] += 1
    return {"start": start, "counts": counts}


def users_overview(db_path):
    with get_db(db_path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "telegram_users" not in tables:
            return []
        profiles = "telegram_user_profiles" in tables
        settings = "telegram_user_settings" in tables
        rows = db.execute(f"""SELECT u.user_id, u.role, u.enabled,
                {"COALESCE(NULLIF(p.display_name,''), NULLIF(p.telegram_name,''), '')" if profiles else "''"},
                {"COALESCE(p.username,'')" if profiles else "''"},
                {"s.notify_mode" if settings else "NULL"}
            FROM telegram_users u
            {"LEFT JOIN telegram_user_profiles p ON p.user_id=u.user_id" if profiles else ""}
            {"LEFT JOIN telegram_user_settings s ON s.user_id=u.user_id" if settings else ""}
            ORDER BY u.role='admin' DESC, u.role='editor' DESC, u.user_id""").fetchall()
    return [{"user_id": uid, "role": role, "enabled": bool(enabled), "name": name, "username": username,
             "mode": effective_mode(role, mode) if enabled else "off"}
            for uid, role, enabled, name, username, mode in rows]


def backups_overview(directory):
    items = []
    for path in auto_backups(directory):
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size, "created": stat.st_mtime})
    return items


def read_diagnostics(path):
    path = Path(path)
    if not path.is_file():
        return {"available":False}
    if path.stat().st_size > 2_000_000:
        raise ValueError("Файл діагностики завеликий")
    data = json.loads(path.read_text())
    return diagnostics_view(data)


def diagnostics_view(data):
    if not isinstance(data, dict):
        raise ValueError("Некоректний знімок діагностики")
    # Telemetry only; ignore any future unrelated root fields and arbitrary strings.
    def numbers(value):
        if value is None or type(value) is bool: return value
        if type(value) in (int,float): return value if math.isfinite(value) else None
        if isinstance(value,list): return [numbers(x) for x in value[:128]]
        if isinstance(value,dict): return {k:numbers(v) for k,v in value.items() if isinstance(k,str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,90}",k)}
        if value in ("ready","ready_fallback","notification","low_rms","empty_text","garbage","blacklist","low_confidence","dictionary_echo","stale","error","whisper_error","discarded","adc","callback","s","ratio",
                     "ok","stuck","failing","loading","failed","disabled"): return value
        return None
    result = {"available":True, **{k:numbers(data[k]) for k in ("schema","started_at","updated_at","ended_at","counters","gauges","timings","asr_latency","notification_spectral") if k in data}}
    if isinstance(data.get("session"), str) and re.fullmatch(r"[0-9a-f]{32}", data["session"]):
        result["session"] = data["session"]
    if data.get("session_status") in ("running", "finished"):
        result["session_status"] = data["session_status"]
    return result


def create_dashboard(db_path, capability, *, port=PORT, diagnostics_path=None, samples_dir=None, bot_token="local-preview", backup_dir=None, recordings_dir=None, launcher_root=None, experiments_dir=None):
    if len(capability)<32:
        raise ValueError("Надто короткий локальний ключ")
    app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
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
                                max_bytes=config.RECORDINGS_MAX_BYTES)
    # Копії лежать поруч із самою базою: для робочої — .local-backups проєкту.
    backup_dir = Path(backup_dir) if backup_dir else Path(db_path).resolve().parent / ".local-backups"
    host = f"127.0.0.1:{port}"
    origin = "http://"+host
    cookie = "radio345_local_admin"
    session = session_value(capability)

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
        response.set_cookie(cookie,session,httponly=True,samesite="strict",max_age=SESSION_DAYS*86400,path="/")
        return response

    @app.get("/admin.js")
    def script(): return Response((ROOT/"local_admin/admin.js").read_text(),media_type="text/javascript")

    @app.get("/styles.css")
    def styles(): return Response((ROOT/"local_admin/styles.css").read_text(),media_type="text/css")

    @app.get("/api/summary")
    def summary(admin=Depends(actor)):
        return {"receiver":read_receiver_status(db_path),"overview":store.overview(db_path),
            "settings":store.read_settings(db_path),"roles":{k:sorted(v) for k,v in PERMISSIONS.items()},
            "telegram_sync":sync_status(db_path)}

    @app.get("/api/overview")
    def overview_page(admin=Depends(actor)):
        """Усе для головної сторінки одним запитом (автооновлення кожні кілька секунд)."""
        return {"receiver": read_receiver_status(db_path), "overview": store.overview(db_path),
                "events_by_hour": events_by_hour(db_path), "users": users_overview(db_path),
                "backups": backups_overview(backup_dir), "telegram_sync": sync_status(db_path),
                "spectral": spectral_settings(),
                "samples": len(list_samples(samples_dir)["items"]),
                "models": {"whisper": config.MODEL_REPO, "qwen": config.LLM_REPO},
                "diagnostics": read_diagnostics(diagnostics_path)}

    @app.post("/api/recordings/suggest-roles")
    def suggest_roles(payload: RoleSuggestion, admin=Depends(actor)):
        """Позивні зі словника, знайдені в тексті оператора, — для швидкого вибору «кому / від».

        Для двох різних позивних пропонуються й ролі (перший — кому, другий — від):
        оператор вирішив, що зручність розмітки важливіша за незалежну оцінку ролей.
        """
        from core.analyzer import fmt
        from services.text_check import dictionaries_from_db
        with get_db(db_path) as db:
            callsigns, _, _ = dictionaries_from_db(db)
        if callsigns is None:
            return {"callsigns": []}
        found = []
        items = callsigns.find(payload.text)
        for item in items:
            text = fmt([item])
            if text not in found:
                found.append(text)
        # Ролі підставляються лише для двох різних позивних в одній передачі
        # (за тим самим правилом, що й у приймачі); інакше оператор вирішує сам.
        roles = None
        from services.text_check import has_several_transmissions
        if len(items) == 2 and not has_several_transmissions(payload.text):
            from core.analyzer import split_roles
            to_found, from_found = split_roles(payload.text, callsigns)
            if len(to_found) == 1 and len(from_found) == 1:
                roles = {"recipient": fmt(to_found), "sender": fmt(from_found)}
        return {"callsigns": found, "roles": roles}

    @app.post("/api/text-check")
    def text_check(payload: TextCheck, admin=Depends(actor)):
        """Зауваження до оформлення тексту еталона; нічого не змінює."""
        from services import daily_codes
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            codes = daily_codes.read(db, daily_codes.local_day(payload.at))
            return check_with_db(db, payload.text, codes)

    @app.get("/api/daily-codes")
    def daily_codes_list(admin=Depends(actor)):
        """Коди доби, внесені вручну, за останні дні."""
        from services import daily_codes
        today = daily_codes.local_day()
        with get_db(db_path) as db:
            permanent = daily_codes.read_permanent(db)
        return {"today": today, "today_period": daily_codes.period(today), "permanent": permanent,
                "change_hour": daily_codes.change_hour(), "items": daily_codes.recent(db_path)}

    @app.put("/api/daily-codes/permanent")
    def daily_codes_permanent(payload: DailyCodesUpdate, admin=Depends(actor)):
        """Постійні коди: діють у будь-яку добу разом із кодами доби."""
        from services import daily_codes
        try:
            return {"codes": daily_codes.write_permanent(db_path, payload.codes)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/daily-codes/{day}")
    def daily_codes_update(day: str, payload: DailyCodesUpdate, admin=Depends(actor)):
        from services import daily_codes
        try:
            codes = daily_codes.write(db_path, day, payload.codes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"day": day, "codes": codes}

    @app.get("/api/recordings")
    def recording_list(request: Request, offset: int = 0, limit: int = 25,
                       q: str = "", edits: str = "", view: str = "all", since: float | None = None,
                       admin=Depends(actor)):
        if (set(request.query_params) - {"offset", "limit", "q", "edits", "view", "since"}
                or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100 or len(q) > 200):
            raise HTTPException(422, "Некоректні параметри")
        if not re.fullmatch(r"(?:|miniapp|[1-9][0-9]{0,17})", edits):
            raise HTTPException(422, "Некоректні параметри")
        from services.recordings import VIEWS
        if (view not in VIEWS) or (since is not None and not 0 <= since <= 4e9):
            raise HTTPException(422, "Некоректні параметри")
        from services import user_activity
        selected = None
        if edits == "miniapp":
            selected = "miniapp"
        elif edits:
            selected = list(user_activity.corrections(db_path, user_id=int(edits)))
        page = recordings.listing(offset, limit, search=q, edits=selected, view=view, since=since)
        page["views"] = recordings.view_counts(since)
        # Хто й коли правив запис у Mini App (журнал дій з 16.11.2026).
        linked = [item["data"].get("fix_id") for item in page["items"] if item["data"].get("fix_id")]
        if linked:
            found = user_activity.corrections(db_path, fix_ids=linked)
            for item in page["items"]:
                if item["data"].get("fix_id") in found:
                    item["corrections"] = found[item["data"]["fix_id"]]
        page["editors"] = user_activity.editors(db_path)
        # Зауваження до оформлення еталонів — щоб картки з ними було видно одразу.
        from services import daily_codes
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            codes_by_day = {}
            for item in page["items"]:
                if item.get("reference") and item.get("label") in ("speech", "unclear"):
                    day = daily_codes.local_day(item["created"])
                    if day not in codes_by_day:
                        codes_by_day[day] = daily_codes.read(db, day)
                    item["text_check"] = check_with_db(db, item["reference"], codes_by_day[day])
        return page

    @app.get("/api/recordings/summary")
    def recording_summary(admin=Depends(actor)):
        return recordings.summary()

    @app.get("/api/recordings/export")
    def recording_export(admin=Depends(actor)):
        with recordings.connect() as db:
            rows = db.execute("SELECT * FROM recordings ORDER BY created").fetchall()
        return Response(json.dumps({"schema": 1, "items": [recordings.decode(row) for row in rows]}, ensure_ascii=False),
                        media_type="application/json", headers={"Content-Disposition": 'attachment; filename="recordings.json"'})

    @app.get("/api/recordings/{record_id}/audio")
    def recording_audio(record_id: str, admin=Depends(actor)):
        try:
            return Response(recordings.audio(record_id), media_type="audio/wav")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.put("/api/recordings/{record_id}/review")
    def recording_review(record_id: str, payload: RecordingReview, admin=Depends(actor)):
        if payload.label == "speech" and not payload.reference.strip():
            raise HTTPException(422, "Введіть почутий текст або позначте нерозбірливий запис")
        if payload.label in ("noise", "notification", "room") and payload.reference.strip():
            raise HTTPException(422, "Для запису без мовлення залиште текст порожнім")
        role_fields = {name: getattr(payload, name) for name in ("reference_recipient", "reference_sender", "multiple_messages")
                       if name in payload.model_fields_set}
        if payload.label in ("noise", "notification", "room") and any(
                isinstance(value, str) and value.strip() for value in role_fields.values()):
            raise HTTPException(422, "Для запису без мовлення позивні не вказуються")
        try:
            result = recordings.review(record_id, payload.reference.strip(), payload.label, payload.expected_revision, **role_fields)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        result["event_sync"] = sync_event_from_reference(result, admin)
        return result

    def sync_event_from_reference(record, admin):
        """Еталон мовлення стає текстом пов'язаної події: оновлюються журнал, підпис у
        Telegram і навчання правил — так само, як після правки в Mini App.

        Повертає None (немає події), "same", "updated", "hidden" або "failed".
        Збій не скасовує збережений еталон.
        """
        fix_id = (record.get("data") or {}).get("fix_id") or ""
        reference = (record.get("reference") or "").strip()
        if record.get("label") not in ("speech", "unclear") or not reference or not re.fullmatch(r"db:[1-9][0-9]{0,17}", fix_id):
            return None
        from services.miniapp_correction_editor import CorrectionConflict, save_edit_draft
        from services.telegram_media_store import is_hidden
        try:
            if is_hidden(db_path, fix_id):
                return "hidden"
            for _ in range(2):   # подію могли змінити між читанням і записом — одна повторна спроба
                with get_db(db_path) as db:
                    row = db.execute("SELECT text FROM radio_events WHERE id=?", (int(fix_id[3:]),)).fetchone()
                if row is None:
                    return None
                if row[0] == reference:
                    return "same"
                try:
                    save_edit_draft(db_path, fix_id, row[0], reference, actor_id=admin)
                    return "updated"
                except CorrectionConflict:
                    continue
            return "failed"
        except Exception:
            logging.getLogger("radio").exception("Не вдалося перенести еталон у подію %s", fix_id)
            return "failed"

    @app.post("/api/recordings/{record_id}/notification-sample")
    def recording_sample(record_id: str, payload: SampleConfirmation, admin=Depends(actor)):
        """Чистий звук сповіщення з архіву стає зразком спектрального фільтра (після перезапуску)."""
        if not payload.confirmed_isolated:
            raise HTTPException(422, "Підтвердь, що у фрагменті немає мовлення")
        try:
            content = recordings.audio(record_id)
        except (LookupError, ValueError):
            raise HTTPException(404, "Аудіо запису недоступне") from None
        try:
            return add_sample(samples_dir, content, confirmed_isolated=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/notification-filter")
    def notification_filter(admin=Depends(actor)):
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {"available": False}
        counters = diagnostics.get("counters") or {}
        return {"spectral": spectral_settings(), "samples": len(list_samples(samples_dir)["items"]),
                # Мітки — з «Аудіоархіву»: справжній PCM приймача, зокрема відкинуті сегменти.
                "labels": notification_eval.label_counts(recordings),
                "updated_at": diagnostics.get("updated_at"), "started_at": diagnostics.get("started_at"),
                "counters": {k: v for k, v in counters.items()
                             if k.startswith("asr.spectral.") or k in ("asr.notification_filtered", "asr.measured_segments", "asr.low_rms")},
                "observed": diagnostics.get("notification_spectral") or {"buckets": [], "recent": []}}

    @app.get("/api/notification-filter/evaluation")
    def notification_evaluation(admin=Depends(actor)):
        limits = spectral_settings()
        return notification_eval.evaluate(recordings, samples_dir, threshold=limits["threshold"],
                                          max_outside_s=limits["max_outside_s"], max_extra=limits["max_extra"])

    @app.put("/api/listening-hold")
    def listening_hold(payload: ListeningHold, admin=Depends(actor)):
        """Поки в панелі звучить запис, приймач не слухає мікрофон (без власної луни)."""
        from services.receiver_control import set_listening_hold
        try:
            return set_listening_hold(db_path, payload.seconds)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/runtime-settings")
    def runtime_settings(admin=Depends(actor)):
        from services.runtime_settings import read_runtime_settings
        result = read_runtime_settings(db_path)
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {}
        gauges = diagnostics.get("gauges") or {}
        result["applied"] = {"revision": gauges.get("runtime_settings.revision"),
                             "receiver": read_receiver_status(db_path)}
        return result

    @app.put("/api/runtime-settings")
    def runtime_settings_update(payload: RuntimeSettingsUpdate, admin=Depends(actor)):
        from services.runtime_settings import RuntimeSettingsConflict, write_runtime_settings
        try:
            return write_runtime_settings(db_path, payload.values, payload.expected_revision)
        except RuntimeSettingsConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/text-lists")
    def text_lists(admin=Depends(actor)):
        from services.text_lists import read_lists
        result = read_lists(db_path)
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {}
        gauges = diagnostics.get("gauges") or {}
        result["applied"] = {"revision": gauges.get("text_lists.revision"),
                             "updated_at": diagnostics.get("updated_at"), "receiver": read_receiver_status(db_path)}
        return result

    @app.get("/api/text-lists/callsign-alias-candidates")
    def callsign_alias_candidates(admin=Depends(actor)):
        """Підказка: почуті Whisper варіанти позивних із розмічених записів. Нічого не записує."""
        from callsigns import Callsigns
        from services.alias_candidates import collect
        from services.text_lists import _dictionary_names, ensure_tables, load_active, sync_aliases
        with get_db(db_path) as db:
            ensure_tables(db)
            sync_aliases(db)
            _, active = load_active(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone():
                return {"items": [], "records": 0}
            lines = [line for (line,) in db.execute("SELECT raw_line FROM dictionary_entries "
                     "WHERE enabled=1 AND dictionary_type='callsigns' ORDER BY line_number")]
            all_names, _ = _dictionary_names(db)
        callsigns = Callsigns.from_lines(lines)
        records = recordings.labelled_speech()
        return {"items": collect(records, callsigns, all_names, active["callsign_aliases"]), "records": len(records)}

    @app.get("/api/text-lists/noise-candidates")
    def noise_candidates(admin=Depends(actor)):
        """Підказка: тексти Whisper, що повторюються на записах без мовлення. Нічого не записує."""
        from services.noise_candidates import collect, key
        from services.text_lists import _dictionary_names, ensure_tables, load_active
        with get_db(db_path) as db:
            ensure_tables(db)
            _, active = load_active(db)
            names, _ = _dictionary_names(db) if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone() else (set(), None)
        keys = {key(v) for v in (*active["blacklist"], *active["short_garbage"])}
        records = recordings.labelled_texts()
        return {"items": collect(records, keys, set(names)), "records": len(records)}

    @app.put("/api/text-lists/{list_type}/check")
    def text_list_check(list_type: str, payload: TextListCheck, admin=Depends(actor)):
        from services.text_lists import check_list
        try:
            return check_list(db_path, list_type, [e.model_dump() for e in payload.entries])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.put("/api/dictionaries/{kind}/check")
    def dictionary_check(kind: str, payload: TextListCheck, admin=Depends(actor)):
        """Перевірка рядків словника без збереження: помилка, яку покаже «Зберегти»."""
        from services.miniapp_management import validate_dictionary
        if kind not in ("callsigns", "commands", "telegram"):
            raise HTTPException(404, "Невідомий словник")
        try:
            validate_dictionary([e.model_dump() for e in payload.entries], kind)
        except ValueError as exc:
            return {"errors": [str(exc)], "warnings": []}
        return {"errors": [], "warnings": []}

    @app.put("/api/text-lists/{list_type}")
    def text_list_update(list_type: str, payload: TextListUpdate, admin=Depends(actor)):
        from services.text_lists import LIST_TYPES, TextListConflict, write_list
        if list_type not in LIST_TYPES:
            raise HTTPException(404, "Невідомий список")
        try:
            return write_list(db_path, list_type, [e.model_dump() for e in payload.entries], payload.expected_revision)
        except TextListConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/dictionaries")
    def dictionaries(admin=Depends(actor)):
        """Словники позивних, команд і Telegram — ті самі, що в Mini App."""
        from services.miniapp_management import read_dictionary
        return {kind: read_dictionary(db_path, kind) for kind in ("callsigns", "commands", "telegram")}

    @app.put("/api/dictionaries/{kind}")
    def dictionary_update(kind: str, payload: DictionaryUpdate, admin=Depends(actor)):
        """Збереження з тією самою перевіркою рядків і захистом від одночасної правки, що й у Mini App."""
        from services.miniapp_management import ManagementConflict, write_dictionary
        if kind not in ("callsigns", "commands", "telegram"):
            raise HTTPException(404, "Невідомий словник")
        try:
            return write_dictionary(db_path, admin, kind, [e.model_dump() for e in payload.entries],
                                    payload.expected_version)
        except ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/learning")
    def learning(admin=Depends(actor)):
        """Навчені заміни слів: що вже діє в розпізнаванні і чого бракує решті."""
        return store.learning_queue(db_path)

    def _rule_call(function, admin, rule_id, *args):
        try:
            return function(db_path, admin, rule_id, *args)
        except store.ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/learning/{rule_id}")
    def learning_toggle(rule_id: int, payload: RuleUpdate, admin=Depends(actor)):
        return _rule_call(store.toggle_rule, admin, rule_id, payload.disabled, payload.expected_version)

    @app.put("/api/learning/{rule_id}/approval")
    def learning_approval(rule_id: int, payload: RuleApproval, admin=Depends(actor)):
        return _rule_call(store.approve_rule, admin, rule_id, payload.approved, payload.expected_version)

    @app.get("/api/live")
    def live(limit: int = 15, admin=Depends(actor)):
        """Стрічка «Ефір зараз»: останні фрази з тим, що з ними сталося."""
        return recordings.recent(max(1, min(limit, 50)))

    @app.get("/api/quality")
    def quality(days: int = 30, admin=Depends(actor)):
        """Якість розпізнавання по днях за еталонами оператора."""
        return {"days": recordings.quality_by_day(max(1, min(days, 365)))}

    @app.get("/api/experiments")
    def experiments(admin=Depends(actor)):
        from services.experiments import list_runs
        return {"items": list_runs(experiments_dir)}

    @app.get("/api/experiments/compare")
    def experiments_compare(a: str, b: str, admin=Depends(actor)):
        from services.experiments import compare_runs
        try:
            return compare_runs(experiments_dir, a, b)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/receiver-pause")
    def receiver_pause(admin=Depends(actor)):
        from services.receiver_control import read_pause_state
        return read_pause_state(db_path)

    @app.put("/api/receiver-pause")
    def receiver_pause_update(payload: PauseUpdate, admin=Depends(actor)):
        """Та сама пауза, що в Mini App: мікрофон закривається на N хвилин, 0 — відновити."""
        from services.receiver_control import PauseVersionConflict, set_pause_minutes
        try:
            return set_pause_minutes(db_path, payload.minutes, expected_revision=payload.expected_revision)
        except PauseVersionConflict:
            raise HTTPException(409, "Стан приймача змінився. Оновіть сторінку.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/attention")
    def attention(admin=Depends(actor)):
        """Що чекає на оператора: коди доби, дозволи замін, збої Telegram, свіжа розмітка."""
        from services import daily_codes
        day = daily_codes.local_day()
        with get_db(db_path) as db:
            codes = daily_codes.read_day(db, day)
        learning = store.learning_queue(db_path)["counts"]
        fresh = recordings.view_counts(time.time() - 86400)
        return {"codes_day": day, "codes_missing": not codes,
                "approvals": learning.get("needs_approval", 0), "conflicts": learning.get("conflict", 0),
                "telegram_failed": sync_status(db_path)["exhausted"],
                "unlabeled_radio_24h": fresh["radio"], "unlabeled_speech_24h": fresh["speech"]}

    @app.get("/api/launcher")
    def launcher_state(admin=Depends(actor)):
        """Чи працює керований лаунчер (без токена керування)."""
        from services.launcher_lifecycle import managed_status
        state = managed_status(launcher_root or ROOT)
        return {"managed": state["managed"], "reachable": state["launcher_reachable"], "state": state["state"],
                "receiver_stopped": "radio" in state.get("stopped_services", [])}

    @app.post("/api/launcher/{command}")
    def launcher_command(command: Literal["restart", "stop", "receiver-stop", "receiver-start"], admin=Depends(actor)):
        """Команда лаунчеру через його захищений канал. Відповідь — одразу: панель
        працює всередині лаунчера й зупиниться разом з усіма службами."""
        from services.launcher_lifecycle import send_command
        try:
            sent = send_command(launcher_root or ROOT, command)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        if not sent:
            raise HTTPException(409, "Лаунчер не керує цим запуском: панель запущено окремо")
        return {"sent": command}

    @app.get("/api/users")
    def users_list(admin=Depends(actor)):
        """Користувачі з версіями для правки (ролі, доступ, назва, режим сповіщень)."""
        return store.list_users(db_path, admin)

    @app.put("/api/users/{user_id}")
    def user_update(user_id: int, payload: UserUpdate, admin=Depends(actor)):
        """Ті самі перевірки, що були в Mini App: версія, останній адміністратор, дозволені режими."""
        try:
            return store.write_user(db_path, admin, user_id, payload.role, payload.enabled,
                                    payload.expected_version, payload.display_name, payload.notify_mode)
        except store.ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/activity")
    def activity(user_id: int | None = None, admin=Depends(actor)):
        """Дії користувача з Mini App і бота (журнал з 16.11.2026) та його статистика."""
        from services import user_activity
        people = user_activity.users(db_path)
        if user_id is None and people:
            user_id = people[0]["user_id"]
        if user_id is not None and all(p["user_id"] != user_id for p in people):
            raise HTTPException(404, "Користувача не знайдено")
        return {"users": people, "selected": user_activity.overview(db_path, user_id) if user_id is not None else None}

    @app.get("/api/backups")
    def backups(admin=Depends(actor)):
        return {"items": backups_overview(backup_dir)}

    @app.post("/api/backups")
    def backup_now(admin=Depends(actor)):
        path = create_backup(db_path, backup_dir)
        return {"name": path.name, "items": backups_overview(backup_dir)}

    @app.get("/api/diagnostics")
    def diagnostics(admin=Depends(actor)): return read_diagnostics(diagnostics_path)

    @app.get("/api/diagnostics/history")
    def diagnostic_sessions(admin=Depends(actor)):
        return diagnostic_history.list_sessions(limit=min(config.DIAGNOSTICS_HISTORY_MAX_SESSIONS, 1000))

    @app.get("/api/diagnostics/history/{session_id}")
    def diagnostic_session(session_id: str, admin=Depends(actor)):
        try:
            return diagnostics_view(diagnostic_history.load(session_id))
        except FileNotFoundError:
            raise HTTPException(404, "Сесію не знайдено або строк зберігання минув") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

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
        from core.notification_spectral import SpectralNotificationMatcher
        content = await upload(request)
        try:
            content,_ = validate_wav(content)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        with wave.open(io.BytesIO(content),"rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        settings = spectral_settings()
        detector = SpectralNotificationMatcher.from_directory(
            samples_dir, 16000, logging.getLogger(__name__),
            **{key: settings[key] for key in ("threshold", "max_outside_s", "max_extra")})
        score = detector.score_pcm(pcm)
        return {"isolated_match": bool(score and score["candidate"]), "templates": len(detector.templates),
                "mode": settings["mode"]}

    @app.get("/api/samples/{sample_id}/audio")
    def sample_audio_file(sample_id: str, admin=Depends(actor)):
        from services.notification_samples import sample_audio
        try:
            return Response(sample_audio(samples_dir, sample_id), media_type="audio/wav")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/notification-filter/dropped")
    def notification_dropped(limit: int = 15, admin=Depends(actor)):
        """Останні записи, які фільтр відкинув як звук сповіщення — для перевірки на слух."""
        return {"items": recordings.by_status("notification", max(1, min(limit, 50)))}

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
    capability = load_capability(rotate=args.rotate_key)
    url = access_url(capability)

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
    app = create_dashboard(config.DB_PATH,capability,recordings_dir=config.RECORDINGS_DIR,bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "local-preview"))
    uvicorn.run(app,host="127.0.0.1",port=PORT,access_log=False,proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
