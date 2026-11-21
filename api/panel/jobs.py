"""Життєвий цикл серверних завдань, незалежно від розділу інтерфейсу."""
from fastapi import Depends, HTTPException


def start_task(ctx, kind, *, actor_id=None, **kwargs):
    from services import panel_jobs
    try:
        return panel_jobs.submit(ctx.db_path, ctx.recordings.root, kind, actor_id=actor_id, **kwargs)
    except panel_jobs.QueueFull as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(503, "Не вдалося зберегти завдання. Перевірте доступність локального сховища.") from None


def register(app, ctx):
    db_path, actor = ctx.db_path, ctx.actor

    @app.get("/api/jobs")
    @app.get("/api/text-jobs")
    def job_list(admin=Depends(actor)):
        from services import panel_jobs
        return {"items": panel_jobs.read(db_path)}

    @app.get("/api/jobs/{job_id}")
    @app.get("/api/text-jobs/{job_id}")
    def job_read(job_id: str, admin=Depends(actor)):
        from services import panel_jobs
        try:
            return panel_jobs.read(db_path, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/jobs/{job_id}/cancel")
    @app.post("/api/text-jobs/{job_id}/cancel")
    def job_cancel(job_id: str, admin=Depends(actor)):
        from services import panel_jobs
        try:
            return panel_jobs.cancel(db_path, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
