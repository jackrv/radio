"""Система: пауза приймача, сховище, копії бази, діагностика."""
from server.api.panel.jobs import start_task
from fastapi import Depends, HTTPException
from typing import Literal

import config
from server.api.panel.models import AudioDevicesUpdate, HomeMoveStart, PauseUpdate
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
    from pathlib import Path
    from server import devices
    device_state = devices.Devices(db_path, Path(db_path).resolve().parent, host=devices.pi_host()) if sys.platform == "darwin" else None

    @app.get("/api/devices")
    def device_list(admin=Depends(actor)):
        """Mac і Pi: служби, база, код. Лише на Mac: з Pi на Mac доступу немає."""
        return device_state.read() if device_state else {"mac": None, "pi": None, "configured": False, "home": None}

    mover = None
    if device_state is not None:
        from server import home_move
        root = Path(db_path).resolve().parent
        mover = home_move.HomeMove(root, home_move.MacSide(root),
                                   home_move.PiSide(device_state.host, root) if device_state.host else None)

    updater = None
    if device_state is not None:
        from server import pi_update
        updater = pi_update.PiUpdate(root, device_state.host)

    @app.get("/api/devices/pi/update")
    def pi_update_state(admin=Depends(actor)):
        return updater.read() if updater else {"phase": "idle", "active": False}

    @app.post("/api/devices/pi/update", status_code=202)
    def pi_update_start(admin=Depends(actor)):
        if updater is None:
            raise HTTPException(409, "Оновлення — лише з панелі на Mac")
        if mover.read().get("active"):
            raise HTTPException(409, "Триває перенесення")
        try:
            updater.start()
        except pi_update.UpdateError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"started": True}

    def need_mover():
        if mover is None:
            raise HTTPException(409, "Перенесення — лише з панелі на Mac")
        return mover

    @app.get("/api/home-move")
    def home_move_state(admin=Depends(actor)):
        return mover.read() if mover else {"phase": "idle"}

    @app.get("/api/home-move/check")
    def home_move_check(target: Literal["mac", "pi"], admin=Depends(actor)):
        return need_mover().check(target)

    @app.post("/api/home-move", status_code=202)
    def home_move_start(payload: HomeMoveStart, admin=Depends(actor)):
        if updater is not None and updater.read()["active"]:
            raise HTTPException(409, "Триває оновлення коду Pi")
        try:
            need_mover().start(payload.target, payload.accept_warnings)
        except home_move.MoveError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"started": True}

    @app.post("/api/home-move/rollback", status_code=202)
    def home_move_rollback(admin=Depends(actor)):
        try:
            need_mover().rollback()
        except home_move.MoveError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"started": True}

    @app.get("/api/devices/{device}/audio")
    def device_audio(device: Literal["mac", "pi"], admin=Depends(actor)):
        if device_state is None:
            raise HTTPException(409, "Керування пристроями — лише з панелі на Mac")
        try:
            return device_state.audio(device)
        except devices.Refused as exc:
            raise HTTPException(409, str(exc)) from None

    @app.put("/api/devices/{device}/audio")
    def device_audio_save(device: Literal["mac", "pi"], payload: AudioDevicesUpdate, admin=Depends(actor)):
        from shared import audio_choice
        if device_state is None:
            raise HTTPException(409, "Керування пристроями — лише з панелі на Mac")
        try:
            return device_state.save_audio(device, payload.values.model_dump(), payload.expected_revision)
        except (devices.Refused, audio_choice.Conflict) as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/devices/{device}/{process}/{verb}")
    def device_action(device: Literal["mac", "pi"], process: Literal["node", "asr", "panel", "api", "tunnel", "worker"],
                      verb: Literal["start", "stop", "restart"], admin=Depends(actor)):
        if device_state is None:
            raise HTTPException(409, "Керування пристроями — лише з панелі на Mac")
        try:
            device_state.act(device, process, verb)
        except devices.Refused as exc:
            raise HTTPException(409, str(exc)) from None
        return {"sent": True}

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
