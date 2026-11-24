"""Аналіз → Експерименти: черга офлайн-тестів лабораторії (рецепти, порядок, запуск і зупинка)."""
import time
import math

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from server.api.panel.common import ROOT


class QueueAdd(BaseModel):
    preset: str = Field(max_length=40)
    note: str = Field(default="", max_length=200)


class QueueMove(BaseModel):
    delta: int = Field(ge=-30, le=30)


class QueueStart(BaseModel):
    start_at: float | None = Field(default=None, ge=0, le=4e9)
    # Вікна часу [[початок, кінець], …] (unix-час): режим «лише запис» вмикається лише в них.
    windows: list[tuple[float, float]] | None = Field(default=None, max_length=8)


class QueueStop(BaseModel):
    now: bool = False


class QueueResume(BaseModel):
    now: bool = True


class QueuePlan(BaseModel):
    windows: list[tuple[float, float]] = Field(max_length=8)


def validated_windows(windows, now):
    windows = sorted(windows)
    if any(not math.isfinite(a) or not math.isfinite(b) or b <= a or b <= now or b > now + 3 * 86400 for a, b in windows):
        raise HTTPException(422, "Вікно має бути непорожнім, не завершеним і в межах 3 діб")
    if any(a < previous[1] for previous, (a, _) in zip(windows, windows[1:])):
        raise HTTPException(422, "Вікна часу не мають перетинатися")
    return windows


def register(app, ctx):
    from shared import lab_queue
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
        from shared.receiver_control import capture_state
        capture = capture_state(ctx.db_path)
        capture["pending_records"] = ctx.recordings.count_status("deferred")
        capture["archive"] = ctx.recordings.capture_summary()
        from server.api.panel.common import read_diagnostics
        try:
            diagnostics = read_diagnostics(ctx.diagnostics_path)
        except (OSError, ValueError):
            diagnostics = {}
        counters = diagnostics.get("counters", {})
        capture["write_errors"] = counters.get("recordings.write_error", 0)
        capture["capture_write_errors"] = counters.get("recordings.capture.write_error", 0)
        for job in data["jobs"]:
            folder = job.get("run_dir") or job.get("resume_dir")
            job["progress"] = lab_queue.progress(root, {"run_dir": folder}) if folder else None
            job["resumable"] = lab_queue.resumable(root, job)
            job["remaining"] = lab_queue.remaining_fraction(root, job) if job["status"] == "queued" else None
        presets = [{"key": key, **{k: v for k, v in p.items() if k != "args"}} for key, p in lab_queue.PRESETS.items()]
        # Оцінка тривалості кожного рецепта — та сама, за якою виконавець вирішує, чи тест уміщається у вікно.
        estimates = {key: lab_queue.estimate_seconds(root, key, records) for key in lab_queue.PRESETS}
        return {"presets": presets, "jobs": data["jobs"], "runner": {**data["runner"], "active": lab_queue.runner_active(data)},
                "cooldown_s": lab_queue.COOLDOWN_S, "margin": lab_queue.WINDOW_MARGIN, "estimates": estimates,
                "records": records, "capture": capture, "now": time.time()}

    @app.get("/api/lab/queue")
    def queue(admin=Depends(actor)):
        return state()

    @app.post("/api/lab/plan")
    def queue_plan(payload: QueuePlan, admin=Depends(actor)):
        now = time.time()
        windows = validated_windows(payload.windows, now)
        records = labelled_records()
        estimates = {key: lab_queue.estimate_seconds(root, key, records) for key in lab_queue.PRESETS}
        return lab_queue.plan_windows(windows, lab_queue.read(root)["jobs"], estimates, now=now, root=root)

    @app.post("/api/lab/queue")
    def queue_add(payload: QueueAdd, admin=Depends(actor)):
        try:
            lab_queue.add(root, payload.preset, payload.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return state()

    @app.post("/api/lab/queue/start")
    def queue_start(payload: QueueStart, admin=Depends(actor)):
        import config
        if lab_queue.runner_active(lab_queue.read(root)):
            raise HTTPException(409, "Черга вже працює. Спершу зупиніть її, щоб змінити розклад.")
        if not config.RECORD_AUDIO:
            raise HTTPException(409, "Для режиму «лише запис» потрібно ввімкнути архів аудіо")
        if not any(j["status"] == "queued" for j in lab_queue.read(root)["jobs"]):
            raise HTTPException(422, "Черга порожня — додайте тест")
        _start(payload.windows or [], payload.start_at)
        return state()

    def _start(windows, start_at):
        import config
        if not config.RECORD_AUDIO:
            raise HTTPException(409, "Для режиму «лише запис» потрібно ввімкнути архів аудіо")
        if not (ROOT / "lab").is_dir():
            raise HTTPException(409, "Лабораторія працює лише на Mac")
        windows = validated_windows(windows, time.time())
        lab_queue.set_windows(root, windows)
        lab_queue.set_start_at(root, None if windows else start_at)
        lab_queue.start(root)

    @app.post("/api/lab/queue/stop")
    def queue_stop(payload: QueueStop, admin=Depends(actor)):
        lab_queue.request_stop(root, now=payload.now)
        return state()

    @app.post("/api/lab/queue/clear")
    def queue_clear(admin=Depends(actor)):
        lab_queue.clear_finished(root)
        return state()

    @app.post("/api/lab/queue/{job_id}/retry")
    def queue_retry(job_id: str, admin=Depends(actor)):
        try:
            lab_queue.retry(root, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return state()

    @app.post("/api/lab/queue/{job_id}/pause")
    def queue_pause(job_id: str, admin=Depends(actor)):
        try:
            lab_queue.pause(root, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return state()

    @app.post("/api/lab/queue/{job_id}/resume")
    def queue_resume(job_id: str, payload: QueueResume, admin=Depends(actor)):
        """Продовжити з того самого запису: одразу (now) або лише повернути в чергу — під розклад."""
        try:
            lab_queue.resume(root, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        if payload.now and not lab_queue.runner_active(lab_queue.read(root)):
            _start([], None)
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
