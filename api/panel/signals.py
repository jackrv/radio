"""Сигнали сповіщень: фільтр, зразки, відкинуті записи."""
import logging

from api.panel.jobs import start_task
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from services.notification_samples import add_sample, delete_sample, list_samples, MAX_UPLOAD_BYTES, validate_wav
from services import notification_eval
from api.panel.common import spectral_settings, read_diagnostics

def register(app, ctx):
    db_path = ctx.db_path
    recordings = ctx.recordings
    actor = ctx.actor
    samples_dir = ctx.samples_dir
    diagnostics_path = ctx.diagnostics_path

    @app.get("/api/notification-filter")
    def notification_filter(admin=Depends(actor)):
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {"available": False}
        counters = diagnostics.get("counters") or {}
        return {"spectral": spectral_settings(db_path), "samples": len(list_samples(samples_dir)["items"]),
                # Мітки — з «Аудіоархіву»: справжній PCM приймача, зокрема відкинуті сегменти.
                "labels": notification_eval.label_counts(recordings),
                "updated_at": diagnostics.get("updated_at"), "started_at": diagnostics.get("started_at"),
                "counters": {k: v for k, v in counters.items()
                             if k.startswith("asr.spectral.") or k in ("asr.notification_filtered", "asr.measured_segments", "asr.low_rms")},
                "observed": diagnostics.get("notification_spectral") or {"buckets": [], "recent": []}}

    @app.post("/api/notification-filter/evaluation", status_code=202)
    def notification_evaluation(admin=Depends(actor)):
        limits = spectral_settings(db_path)
        return start_task(ctx, "notification_evaluation", actor_id=admin,
                                 options={"samples_dir":str(samples_dir), "limits":{k:limits[k] for k in ("threshold", "max_outside_s", "max_extra")}})

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
            return await run_in_threadpool(add_sample, samples_dir, content, confirmed_isolated=request.headers.get("x-confirm-isolated")=="yes")
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    @app.post("/api/samples/test")
    async def sample_test(request: Request, admin=Depends(actor)):
        content = await upload(request)
        return await run_in_threadpool(score_sample, content)

    def score_sample(content):
        import io
        import logging
        import wave
        from core.notification_spectral import SpectralNotificationMatcher
        try:
            content,_ = validate_wav(content)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        with wave.open(io.BytesIO(content),"rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        settings = spectral_settings(db_path)
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
    def notification_dropped(limit: int = 15, reviewed: bool = False, admin=Depends(actor)):
        """Відкинуті фільтром як звук сповіщення — для перевірки на слух.

        Типово лише ще не розмічені (розмічене зникає); reviewed=true — усі останні.
        """
        limit = max(1, min(limit, 50))
        return {"items": recordings.by_status("notification", limit, unreviewed=not reviewed),
                "unreviewed": recordings.count_status("notification", unreviewed=True),
                "total": recordings.count_status("notification")}

    @app.delete("/api/samples/{sample_id}")
    def sample_delete(sample_id: str,admin=Depends(actor)):
        try:
            return delete_sample(samples_dir, sample_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
