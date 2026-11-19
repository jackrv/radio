"""Аналіз → Експерименти: черга офлайн-тестів лабораторії (рецепти, порядок, запуск і зупинка)."""
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from api.panel.common import ROOT


class QueueAdd(BaseModel):
    preset: str = Field(max_length=40)
    note: str = Field(default="", max_length=200)


class QueueMove(BaseModel):
    delta: int = Field(ge=-30, le=30)


class QueueStart(BaseModel):
    start_at: float | None = Field(default=None, ge=0, le=4e9)
    # Вікна часу [[початок, кінець], …] (unix-час): приймач зупиняється лише в них.
    windows: list[tuple[float, float]] | None = Field(default=None, max_length=8)


class QueueStop(BaseModel):
    now: bool = False


def register(app, ctx):
    from services import lab_queue
    actor, root, launcher_root = ctx.actor, ctx.experiments_dir, ctx.launcher_root

    def labelled_records():
        try:
            with ctx.recordings.connect() as db:
                return db.execute("SELECT COUNT(*) FROM recordings WHERE reference IS NOT NULL").fetchone()[0]
        except Exception:
            return 1000

    def state():
        data = lab_queue.read(root)
        records = labelled_records()
        for job in data["jobs"]:
            job["progress"] = lab_queue.progress(root, job) if job["status"] in ("running", "done", "failed", "cancelled") else None
        presets = [{"key": key, **{k: v for k, v in p.items() if k != "args"}} for key, p in lab_queue.PRESETS.items()]
        # Оцінка тривалості кожного рецепта — та сама, за якою виконавець вирішує, чи тест уміщається у вікно.
        estimates = {key: lab_queue.estimate_seconds(root, key, records) for key in lab_queue.PRESETS}
        return {"presets": presets, "jobs": data["jobs"], "runner": {**data["runner"], "active": lab_queue.runner_active(data)},
                "cooldown_s": lab_queue.COOLDOWN_S, "margin": lab_queue.WINDOW_MARGIN, "estimates": estimates,
                "records": records, "now": time.time()}

    @app.get("/api/lab/queue")
    def queue(admin=Depends(actor)):
        return state()

    @app.post("/api/lab/queue")
    def queue_add(payload: QueueAdd, admin=Depends(actor)):
        try:
            lab_queue.add(root, payload.preset, payload.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return state()

    @app.post("/api/lab/queue/start")
    def queue_start(payload: QueueStart, admin=Depends(actor)):
        if not any(j["status"] == "queued" for j in lab_queue.read(root)["jobs"]):
            raise HTTPException(422, "Черга порожня — додайте тест")
        now = time.time()
        windows = [(a, b) for a, b in (payload.windows or [])]
        if any(b <= a or b < now or a > now + 3 * 86400 for a, b in windows):
            raise HTTPException(422, "Вікно має закінчуватися пізніше, ніж починається, і не раніше, ніж зараз (до 3 діб наперед)")
        lab_queue.set_windows(root, windows)
        lab_queue.set_start_at(root, None if windows else payload.start_at)
        lab_queue.start(root, launcher_root=launcher_root or ROOT)
        return state()

    @app.post("/api/lab/queue/stop")
    def queue_stop(payload: QueueStop, admin=Depends(actor)):
        lab_queue.request_stop(root, now=payload.now)
        return state()

    @app.post("/api/lab/queue/clear")
    def queue_clear(admin=Depends(actor)):
        lab_queue.clear_finished(root)
        return state()

    @app.post("/api/lab/queue/{job_id}/move")
    def queue_move(job_id: str, payload: QueueMove, admin=Depends(actor)):
        try:
            lab_queue.move(root, job_id, payload.delta)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return state()

    @app.delete("/api/lab/queue/{job_id}")
    def queue_cancel(job_id: str, admin=Depends(actor)):
        try:
            lab_queue.cancel(root, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return state()
