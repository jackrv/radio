"""Система: пауза приймача, сховище, копії бази, діагностика."""
from server.api.panel.jobs import start_task
from fastapi import Depends, HTTPException
from typing import Literal

import config
from server.api.panel.models import PauseUpdate
from server.api.panel.common import backups_overview, read_diagnostics, diagnostics_view

def register(app, ctx):
    db_path = ctx.db_path
    recordings = ctx.recordings
    actor = ctx.actor
    diagnostics_path = ctx.diagnostics_path
    diagnostic_history = ctx.diagnostic_history
    backup_dir = ctx.backup_dir

    @app.post('/api/telegram-sync/{action}')
    def telegram_sync_action(action: Literal['retry','skip'], admin=Depends(actor)):
        from shared.telegram_sync import resolve_failed_corrections
        try:
            return resolve_failed_corrections(db_path, admin, action)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    @app.get("/api/receiver-pause")
    def receiver_pause(admin=Depends(actor)):
        from shared.receiver_control import read_pause_state
        return read_pause_state(db_path)

    @app.put("/api/receiver-pause")
    def receiver_pause_update(payload: PauseUpdate, admin=Depends(actor)):
        """Та сама пауза, що в Mini App: мікрофон закривається на N хвилин, 0 — відновити."""
        from shared.receiver_control import PauseVersionConflict, set_pause_minutes
        try:
            return set_pause_minutes(db_path, payload.minutes, expected_revision=payload.expected_revision)
        except PauseVersionConflict:
            raise HTTPException(409, "Стан приймача змінився. Оновіть сторінку.") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    import sys
    from server import devices
    device_state = devices.Devices(db_path, host=devices.pi_host()) if sys.platform == "darwin" else None

    @app.get("/api/devices")
    def device_list(admin=Depends(actor)):
        """Mac і Pi: служби, база, код. Лише на Mac: з Pi на Mac доступу немає."""
        return device_state.read() if device_state else {"mac": None, "pi": None, "configured": False, "home": None}

    @app.get("/api/storage")
    def storage(admin=Depends(actor)):
        from server.storage_status import storage_status
        return storage_status(db_path, recordings, diagnostic_history, backup_dir)

    @app.get("/api/backups")
    def backups(admin=Depends(actor)):
        return {"items": backups_overview(backup_dir)}


    @app.post('/api/archive-inventory', status_code=202)
    def archive_inventory(admin=Depends(actor)):
        return start_task(ctx, 'archive_inventory', actor_id=admin)

    @app.post("/api/backups", status_code=202)
    def backup_now(admin=Depends(actor)):
        return start_task(ctx, "backup", actor_id=admin,
                                 options={"backup_dir": str(backup_dir)})

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
