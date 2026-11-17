"""Окрема локальна адмінпанель. Ніколи не монтувати на публічному API/ngrok."""

import argparse
import hashlib
import hmac
import json
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
from pydantic import BaseModel, Field
import uvicorn

import config
from api.app import create_app
from api.management import SettingsUpdate
from services import miniapp_management as store
from services.notification_samples import add_sample, delete_sample, list_samples, MAX_UPLOAD_BYTES, validate_wav
from services import notification_eval
from services.receiver_status import read_receiver_status
from services.sqlite_connection import get_db
from services.user_store import PERMISSIONS
from services.telegram_sync import sync_status
from services.db_backup import auto_backups, create_backup
from services.diagnostic_history import DiagnosticsHistory, history_directory
from services.miniapp_correction_editor import load_edit_draft
from services.telegram_audio import AudioUnavailable, download_voice
from services.telegram_media_store import audio_file_id
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


class ListeningHold(BaseModel):
    seconds: float = Field(ge=0, le=120)


class RuntimeSettingsUpdate(BaseModel):
    values: dict[str, bool | int | float]
    expected_revision: int = Field(ge=0)


class TextListEntry(BaseModel):
    raw_line: str = Field(max_length=300)
    enabled: bool = Field(strict=True)


class TextListUpdate(BaseModel):
    entries: list[TextListEntry] = Field(max_length=500)
    expected_revision: int = Field(ge=0)


class DictionaryEntry(BaseModel):
    raw_line: str = Field(max_length=500)
    enabled: bool = Field(strict=True)


class DictionaryUpdate(BaseModel):
    entries: list[DictionaryEntry] = Field(max_length=1000)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class PhraseSample(BaseModel):
    fix_id: str = Field(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$")
    confirmed_isolated: bool = Field(strict=True)


def spectral_settings():
    return {"mode": getattr(config, "SPECTRAL_NOTIFICATION_MODE", "off"),
            "threshold": float(getattr(config, "SPECTRAL_NOTIFICATION_THRESHOLD", 0.85)),
            "max_outside_s": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S", 0.2)),
            "max_extra": float(getattr(config, "SPECTRAL_NOTIFICATION_MAX_EXTRA", 0.02))}


def _utc_seconds(text):
    from datetime import datetime, timezone
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def attach_spectral(items, recent, tolerance=3.0):
    """Підписує фразу оцінкою спектрального фільтра, якщо її сегмент є серед недавніх високих.

    Кожен сегмент дістається не більш ніж одній фразі (найближчій у часі).
    """
    ready = [row for row in recent or () if isinstance(row, dict)
             and isinstance(row.get("text_ready_unix_s"), (int, float)) and isinstance(row.get("notification_score"), (int, float))]
    pairs = []
    for position, item in enumerate(items):
        created = _utc_seconds(item.get("created_at"))
        if created is None:
            continue
        for index, row in enumerate(ready):
            # Подія записується після готовності тексту; ts у базі округлено до секунди.
            distance = abs(row["text_ready_unix_s"] - created - 0.5)
            if distance <= tolerance:
                pairs.append((distance, position, index))
    taken_items, taken_rows = set(), set()
    for _, position, index in sorted(pairs):
        if position in taken_items or index in taken_rows:
            continue
        taken_items.add(position); taken_rows.add(index)
        row = ready[index]
        items[position]["spectral"] = {"score": row["notification_score"], "outside_s": row.get("notification_outside_s"),
                                       "extra_ratio": row.get("notification_extra"),
                                       "candidate": bool(row.get("notification_candidate"))}
    return items


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


def create_dashboard(db_path, capability, *, port=PORT, diagnostics_path=None, samples_dir=None, bot_token="local-preview", backup_dir=None, recordings_dir=None):
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
    from services.recordings import RecordingStore, REVIEW_QUEUES
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
            "notification_filter":bool(config.FILTER_NOTIFICATION_SOUNDS),
            "telegram_sync":sync_status(db_path)}

    @app.get("/api/overview")
    def overview_page(admin=Depends(actor)):
        """Усе для головної сторінки одним запитом (автооновлення кожні кілька секунд)."""
        return {"receiver": read_receiver_status(db_path), "overview": store.overview(db_path),
                "events_by_hour": events_by_hour(db_path), "users": users_overview(db_path),
                "backups": backups_overview(backup_dir), "telegram_sync": sync_status(db_path),
                "notification_filter": bool(config.FILTER_NOTIFICATION_SOUNDS),
                "spectral": spectral_settings(),
                "samples": len(list_samples(samples_dir)["items"]),
                "models": {"whisper": config.MODEL_REPO, "qwen": config.LLM_REPO},
                "diagnostics": read_diagnostics(diagnostics_path)}

    callsign_cache = {}

    @app.post("/api/recordings/suggest-roles")
    def suggest_roles(payload: RoleSuggestion, admin=Depends(actor)):
        """Позивні зі словника, знайдені в тексті оператора, — для швидкого вибору «кому / від».

        Для двох різних позивних пропонуються й ролі (перший — кому, другий — від):
        оператор вирішив, що зручність розмітки важливіша за незалежну оцінку ролей.
        """
        from callsigns import Callsigns
        from core.analyzer import fmt
        from services.dictionary_loader import dictionary_fingerprint
        from services.text_lists import sync_aliases
        with get_db(db_path) as db:
            sync_aliases(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone():
                return {"callsigns": []}
            key = dictionary_fingerprint(db)
            if key not in callsign_cache:
                lines = [line for (line,) in db.execute("SELECT raw_line FROM dictionary_entries "
                         "WHERE enabled=1 AND dictionary_type='callsigns' ORDER BY line_number")]
                callsign_cache.clear()
                callsign_cache[key] = Callsigns.from_lines(lines)
            callsigns = callsign_cache[key]
        found = []
        items = callsigns.find(payload.text)
        for item in items:
            text = fmt([item])
            if text not in found:
                found.append(text)
        # Ролі підставляються лише для двох різних позивних в одній передачі
        # (за тим самим правилом, що й у приймачі); інакше оператор вирішує сам.
        roles = None
        if len(items) == 2 and "|" not in payload.text:
            from core.analyzer import split_roles
            to_found, from_found = split_roles(payload.text, callsigns)
            if len(to_found) == 1 and len(from_found) == 1:
                roles = {"recipient": fmt(to_found), "sender": fmt(from_found)}
        return {"callsigns": found, "roles": roles}

    @app.post("/api/text-check")
    def text_check(payload: TextCheck, admin=Depends(actor)):
        """Зауваження до оформлення тексту еталона; нічого не змінює."""
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            return check_with_db(db, payload.text)

    @app.get("/api/recordings/queue")
    def recording_queue(admin=Depends(actor)):
        return {"available": True, "live": True, "updated_at": time.time(), "groups": recordings.queue_progress()}

    @app.get("/api/recordings")
    def recording_list(offset: int = 0, limit: int = 25, review: str = "all", group: str = "all", queue: str = "",
                       q: str = "", edits: str = "", admin=Depends(actor)):
        if (not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100 or review not in ("all", "pending", "reviewed")
                or group not in ("all", "normal", "short", "shadow", "speech", "radio") or not re.fullmatch(r"(?:|all|[A-Z])", queue)
                or len(q) > 200):
            raise HTTPException(422, "Некоректні параметри")
        if queue and queue != "all" and queue not in REVIEW_QUEUES and not q.strip():
            raise HTTPException(404, "Такої групи в черзі немає")
        if not re.fullmatch(r"(?:|miniapp|[1-9][0-9]{0,17})", edits):
            raise HTTPException(422, "Некоректні параметри")
        from services import user_activity
        selected = None
        if edits == "miniapp":
            selected = "miniapp"
        elif edits:
            selected = list(user_activity.corrections(db_path, user_id=int(edits)))
        page = recordings.listing(offset, limit, review, group, queue=queue, search=q, edits=selected)
        # Хто й коли правив запис у Mini App (журнал дій з 16.11.2026).
        linked = [item["data"].get("fix_id") for item in page["items"] if item["data"].get("fix_id")]
        if linked:
            found = user_activity.corrections(db_path, fix_ids=linked)
            for item in page["items"]:
                if item["data"].get("fix_id") in found:
                    item["corrections"] = found[item["data"]["fix_id"]]
        page["editors"] = user_activity.editors(db_path)
        # Зауваження до оформлення еталонів — щоб картки з ними було видно одразу.
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            for item in page["items"]:
                if item.get("reference") and item.get("label") in ("speech", "unclear"):
                    item["text_check"] = check_with_db(db, item["reference"])
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
            return recordings.review(record_id, payload.reference.strip(), payload.label, payload.expected_revision, **role_fields)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/phrases")
    def phrases(q: str = "", offset: int = 0, limit: int = 30, admin=Depends(actor)):
        if len(q) > 200 or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
            raise HTTPException(422, "Некоректні параметри пошуку")
        page = store.list_phrases(db_path, q, offset, limit)
        try:
            recent = (read_diagnostics(diagnostics_path).get("notification_spectral") or {}).get("recent")
        except ValueError:
            recent = None
        attach_spectral(page["items"], recent)
        return page

    def phrase_audio(fix_id):
        if not re.fullmatch(r"(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})", fix_id):
            raise HTTPException(422, "Некоректний ідентифікатор фрази")
        try:
            load_edit_draft(db_path, fix_id)
            file_id = audio_file_id(db_path, fix_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        try:
            return download_voice(bot_token, file_id)
        except AudioUnavailable:
            raise HTTPException(502, "Не вдалося отримати аудіо з Telegram") from None

    @app.get("/api/phrases/audio")
    def audio(fix_id: str, admin=Depends(actor)):
        return Response(phrase_audio(fix_id), media_type="audio/ogg")

    @app.put("/api/phrases/notification-sample")
    def phrase_sample(payload: PhraseSample, admin=Depends(actor)):
        if not payload.confirmed_isolated:
            raise HTTPException(422, "Підтвердь, що у фрагменті немає мовлення")
        from services.notification_samples import ogg_to_wav
        try:
            return add_sample(samples_dir, ogg_to_wav(phrase_audio(payload.fix_id)), confirmed_isolated=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            raise HTTPException(502, "Не вдалося перетворити аудіо") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/notification-filter")
    def notification_filter(admin=Depends(actor)):
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {"available": False}
        counters = diagnostics.get("counters") or {}
        return {"spectral": spectral_settings(), "waveform": bool(config.FILTER_NOTIFICATION_SOUNDS),
                "samples": len(list_samples(samples_dir)["items"]),
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
