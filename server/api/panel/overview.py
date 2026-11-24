"""Огляд: стан, жива стрічка, якість, експерименти, «Потребує уваги», лаунчер."""
import time
from typing import Literal

from server.api.panel.jobs import start_task
from fastapi import Depends, HTTPException

import config
from shared.management.learning import learning_queue
from server.management.overview import overview
from server.management.settings import read_settings
from server.notification_samples import list_samples
from server.receiver_status import read_receiver_status
from shared.sqlite_connection import get_db
from shared.user_store import PERMISSIONS
from shared.telegram_sync import sync_status
from server.api.panel.models import SuggestionDismiss, SuggestionMute, SuggestionRestore
from server.api.panel.common import ROOT, spectral_settings, events_by_hour, users_overview, backups_overview, read_diagnostics

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

    @app.get("/api/quality/errors")
    def quality_errors(days: int = 0, admin=Depends(actor)):
        """Що заважає тексту «без правок»: види розбіжностей, вплив обробки, найчастіші заміни (days=0 — увесь архів)."""
        from shared.error_analysis import analyse, callsign_names, load_records
        records = load_records(recordings, days)
        with get_db(db_path) as db:
            names = callsign_names(db)
        return analyse(records, names)

    @app.post("/api/quality/text-impact", status_code=202)
    def quality_text_impact(admin=Depends(actor)):
        """Фонова контр-перевірка; результат доступний після закриття сторінки."""
        try:
            return start_task(ctx, "impact")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/quality")
    def quality(days: int = 30, admin=Depends(actor)):
        """Якість розпізнавання по днях за еталонами оператора."""
        return {"days": recordings.quality_by_day(max(1, min(days, 365)))}

    @app.get("/api/training")
    def training(admin=Depends(actor)):
        """Скільки розмічено для дообучення Whisper (орієнтири — 2 і 5 годин мовлення)."""
        from shared.training_set import readiness
        return readiness(recordings)

    @app.post("/api/training/export", status_code=202)
    def training_export(admin=Depends(actor)):
        return start_task(ctx, "training_export", actor_id=admin)

    @app.get("/api/experiments")
    def experiments(admin=Depends(actor)):
        from shared.experiments import list_runs
        return {"items": list_runs(experiments_dir)}

    @app.get("/api/experiments/compare")
    def experiments_compare(a: str, b: str, admin=Depends(actor)):
        from shared.experiments import compare_runs
        try:
            return compare_runs(experiments_dir, a, b)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/experiments/{name}")
    def experiment_report(name: str, admin=Depends(actor)):
        from shared.experiments import report
        try:
            return report(experiments_dir, name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except (LookupError, OSError):
            raise HTTPException(404, "Звіт недоступний") from None

    @app.get("/api/attention")
    def attention(admin=Depends(actor)):
        """Що чекає на оператора: коди доби, дозволи замін, збої Telegram, свіжа розмітка."""
        from shared import daily_codes
        day = daily_codes.local_day()
        with get_db(db_path) as db:
            codes = daily_codes.read_day(db, day)
        queue = learning_queue(db_path)
        learning = queue["counts"]
        # Самі заміни — щоб у «Потребує уваги» було видно, що саме чекає (до 3 прикладів).
        pairs = lambda state: [f"«{i['original']}» → «{i['corrected']}»" for i in queue["items"] if i["state"] == state][:3]
        fresh = recordings.view_counts(time.time() - 86400)
        from shared import suggestions as suggestion_service
        found = suggestion_service.overview(db_path)
        return {"suggestions": found["groups"], "suggestions_hidden": found["hidden"], "suggestion_kinds": found["kinds"],
                "suggestions_pending": bool(suggestion_worker and suggestion_worker.pending),
                "codes_day": day, "codes_missing": not codes,
                "approvals": learning.get("needs_approval", 0), "conflicts": learning.get("conflict", 0),
                "approval_examples": pairs("needs_approval"), "conflict_examples": pairs("conflict"),
                "telegram_failed": sync_status(db_path)["exhausted"],
                "unlabeled_radio_24h": fresh["radio"], "unlabeled_speech_24h": fresh["speech"],
                "lab_running": lab_running()}

    def lab_running():
        """Іде черга тестів лабораторії (приймач слухає, але Whisper вивантажено)."""
        from shared import lab_queue
        try:
            return lab_queue.runner_active(lab_queue.read(experiments_dir))
        except OSError:
            return False

    @app.get("/api/workdesk")
    def workdesk(admin=Depends(actor)):
        """Робочий стіл Огляду: черги розмітки, якість за 7 днів і коди поточної доби (оновлення раз на хвилину)."""
        from shared import daily_codes
        day = daily_codes.local_day()
        with get_db(db_path) as db:
            codes = daily_codes.read_day(db, day)
        counts, fresh = recordings.view_counts(), recordings.view_counts(time.time() - 86400)
        return {"queue": {"radio_24h": fresh["radio"], "no_roles": counts["no_roles"], "false_text": counts["false_text"],
                          "missed": counts["missed"], "unclear": counts["unclear"]},
                "quality": recordings.quality_by_day(7),
                "codes": {"day": day, "period": daily_codes.period(day), "codes": codes}}

    @app.post("/api/suggestions/sync-events", status_code=202)
    def suggestions_sync_events(admin=Depends(actor)):
        return start_task(ctx, "sync_events", actor_id=admin)

    @app.post("/api/suggestions/dismiss")
    def suggestions_dismiss(payload: SuggestionDismiss, admin=Depends(actor)):
        """«Готово»: оператор бачив пропозиції цього виду й лишає як є."""
        from shared import suggestions as suggestion_service
        if payload.kind not in suggestion_service.KINDS:
            raise HTTPException(422, "Невідомий вид пропозицій")
        return {"dismissed": suggestion_service.dismiss(db_path, payload.kind)}

    @app.post("/api/suggestions/mute")
    def suggestions_mute(payload: SuggestionMute, admin=Depends(actor)):
        """«Не перевіряти»: вид пропозицій більше не обчислюється й не показується (вмикається в «Система»)."""
        from shared import suggestions as suggestion_service
        try:
            return suggestion_service.set_muted(db_path, payload.kind, payload.muted)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/suggestions/restore")
    def suggestions_restore(payload: SuggestionRestore | None = None, admin=Depends(actor)):
        from shared import suggestions as suggestion_service
        return {"restored": suggestion_service.restore(db_path, keys=payload.keys if payload else None)}

    @app.get('/api/suggestions/resolved')
    def suggestion_decisions(offset: int=0, admin=Depends(actor)):
        from shared.suggestions import decisions
        if not 0 <= offset <= 1_000_000: raise HTTPException(422, 'Некоректна сторінка')
        return decisions(db_path, offset)

    @app.post("/api/suggestions/refresh", status_code=202)
    def suggestions_refresh(admin=Depends(actor)):
        """Перевірити всі списки зараз (зокрема шаблони фраз на еталонах), не чекаючи фонової перевірки."""
        try:
            return start_task(ctx, "suggestions", deep=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/launcher")
    def launcher_state(admin=Depends(actor)):
        """Хто веде служби: r345 (є кнопки), systemd на Pi або ніхто."""
        import os
        from shared.r345_control import Control
        running = Control((launcher_root or ROOT) / ".radio345-launcher").read()
        if running:
            return {"mode": "r345", "profile": running.get("profile"),
                    "stopped": sorted(n for n, p in running.get("processes", {}).items() if not p.get("alive") and p.get("wanted", True))}
        return {"mode": "systemd" if os.environ.get("INVOCATION_ID") else "none", "profile": None, "stopped": []}

    @app.post("/api/launcher/{command}")
    def launcher_command(command: Literal["restart", "stop"], admin=Depends(actor)):
        """Відповідь — одразу: панель працює під r345 і зупиниться разом з усіма службами."""
        from shared.r345_control import Control
        control = Control((launcher_root or ROOT) / ".radio345-launcher")
        running = control.read()
        if running is None:
            raise HTTPException(409, "Службами керує не r345")
        if command == "stop":
            control.request_stop()
        else:
            control.request("restart", [n for n, p in running["processes"].items() if p.get("wanted", True)])
        return {"sent": command}
