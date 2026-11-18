"""Огляд: стан, жива стрічка, якість, експерименти, «Потребує уваги», лаунчер."""
import time
from typing import Literal

from fastapi import Depends, HTTPException

import config
from services.management.learning import learning_queue
from services.management.overview import overview
from services.management.settings import read_settings
from services.notification_samples import list_samples
from services.receiver_status import read_receiver_status
from services.sqlite_connection import get_db
from services.user_store import PERMISSIONS
from services.telegram_sync import sync_status
from api.panel.models import SuggestionDismiss
from api.panel.common import ROOT, spectral_settings, events_by_hour, users_overview, backups_overview, read_diagnostics

def register(app, ctx):
    db_path = ctx.db_path
    recordings = ctx.recordings
    actor = ctx.actor
    samples_dir = ctx.samples_dir
    diagnostics_path = ctx.diagnostics_path
    experiments_dir = ctx.experiments_dir
    suggestion_worker = ctx.suggestion_worker
    backup_dir = ctx.backup_dir
    launcher_root = ctx.launcher_root
    sync_event_from_reference = ctx.sync_event_from_reference

    @app.get("/api/summary")
    def summary(admin=Depends(actor)):
        return {"receiver":read_receiver_status(db_path),"overview":overview(db_path),
            "settings":read_settings(db_path),"roles":{k:sorted(v) for k,v in PERMISSIONS.items()},
            "telegram_sync":sync_status(db_path)}

    @app.get("/api/overview")
    def overview_page(admin=Depends(actor)):
        """Усе для головної сторінки одним запитом (автооновлення кожні кілька секунд)."""
        return {"receiver": read_receiver_status(db_path), "overview": overview(db_path),
                "events_by_hour": events_by_hour(db_path), "users": users_overview(db_path),
                "backups": backups_overview(backup_dir), "telegram_sync": sync_status(db_path),
                "spectral": spectral_settings(db_path),
                "samples": len(list_samples(samples_dir)["items"]),
                "models": {"whisper": config.MODEL_REPO, "qwen": config.LLM_REPO},
                "diagnostics": read_diagnostics(diagnostics_path)}

    @app.get("/api/live")
    def live(limit: int = 15, admin=Depends(actor)):
        """Стрічка «Ефір зараз»: останні фрази з тим, що з ними сталося."""
        return recordings.recent(max(1, min(limit, 50)))

    @app.get("/api/quality")
    def quality(days: int = 30, admin=Depends(actor)):
        """Якість розпізнавання по днях за еталонами оператора."""
        return {"days": recordings.quality_by_day(max(1, min(days, 365)))}

    @app.get("/api/training")
    def training(admin=Depends(actor)):
        """Скільки розмічено для дообучення Whisper (орієнтири — 2 і 5 годин мовлення)."""
        from services.training_set import readiness
        return readiness(recordings)

    @app.post("/api/training/export")
    def training_export(admin=Depends(actor)):
        """Локальне вивантаження набору (.local-training/), нікуди не надсилається."""
        from pathlib import Path
        from services.training_set import export
        try:
            return export(recordings, Path(db_path).resolve().parent / ".local-training")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

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

    @app.get("/api/attention")
    def attention(admin=Depends(actor)):
        """Що чекає на оператора: коди доби, дозволи замін, збої Telegram, свіжа розмітка."""
        from services import daily_codes
        day = daily_codes.local_day()
        with get_db(db_path) as db:
            codes = daily_codes.read_day(db, day)
        learning = learning_queue(db_path)["counts"]
        fresh = recordings.view_counts(time.time() - 86400)
        from services import suggestions as suggestion_service
        found = suggestion_service.overview(db_path)
        return {"suggestions": found["groups"], "suggestions_hidden": found["hidden"],
                "suggestions_pending": bool(suggestion_worker and suggestion_worker.pending),
                "codes_day": day, "codes_missing": not codes,
                "approvals": learning.get("needs_approval", 0), "conflicts": learning.get("conflict", 0),
                "telegram_failed": sync_status(db_path)["exhausted"],
                "unlabeled_radio_24h": fresh["radio"], "unlabeled_speech_24h": fresh["speech"]}

    @app.get("/api/workdesk")
    def workdesk(admin=Depends(actor)):
        """Робочий стіл Огляду: черги розмітки, якість за 7 днів і коди поточної доби (оновлення раз на хвилину)."""
        from services import daily_codes
        day = daily_codes.local_day()
        with get_db(db_path) as db:
            codes = daily_codes.read_day(db, day)
        counts, fresh = recordings.view_counts(), recordings.view_counts(time.time() - 86400)
        return {"queue": {"radio_24h": fresh["radio"], "no_roles": counts["no_roles"], "false_text": counts["false_text"],
                          "missed": counts["missed"], "unclear": counts["unclear"]},
                "quality": recordings.quality_by_day(7),
                "codes": {"day": day, "period": daily_codes.period(day), "codes": codes}}

    @app.post("/api/suggestions/sync-events")
    def suggestions_sync_events(admin=Depends(actor)):
        """Перенести еталони в події, які лишилися з текстом Whisper (журнал, підпис у Telegram, навчання)."""
        from services import suggestions as suggestion_service
        result = {"updated": 0, "same": 0, "hidden": 0, "failed": 0, "none": 0}
        for item in suggestion_service.reference_checks(db_path, recordings):
            if item["kind"] != "ref_event":
                continue
            try:
                outcome = sync_event_from_reference(recordings.get(item["record"]), admin)
            except LookupError:
                outcome = None
            result[outcome or "none"] += 1
        suggestion_service.refresh(db_path, recordings)
        return result

    @app.post("/api/suggestions/dismiss")
    def suggestions_dismiss(payload: SuggestionDismiss, admin=Depends(actor)):
        """«Готово»: оператор бачив пропозиції цього виду й лишає як є."""
        from services import suggestions as suggestion_service
        if payload.kind not in suggestion_service.KINDS:
            raise HTTPException(422, "Невідомий вид пропозицій")
        return {"dismissed": suggestion_service.dismiss(db_path, payload.kind)}

    @app.post("/api/suggestions/restore")
    def suggestions_restore(admin=Depends(actor)):
        from services import suggestions as suggestion_service
        return {"restored": suggestion_service.restore(db_path)}

    @app.post("/api/suggestions/refresh")
    def suggestions_refresh(admin=Depends(actor)):
        """Перевірити всі списки зараз (зокрема шаблони фраз на еталонах), не чекаючи фонової перевірки."""
        from services import suggestions as suggestion_service
        current, fresh = suggestion_service.refresh(db_path, recordings, deep=True)
        return {"total": len(current), "new": len(fresh), **suggestion_service.overview(db_path)}

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
