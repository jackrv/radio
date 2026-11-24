"""Authenticated Mini App routes beyond the core API; no runtime model imports."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from services.management.phrases import list_phrases
from shared.miniapp_correction_editor import load_edit_draft
from shared.telegram_media_store import audio_file_id
from services.telegram_audio import download_voice, AudioUnavailable


def management_router(db_path, current_user, check_permission, bot_token):
    """Лише те, що потрібно з телефона: журнал фраз і їх аудіо.

    Словники, навчені заміни, користувачі й налаштування розпізнавання керуються
    з локальної панелі (127.0.0.1) і через ngrok не відкриваються.
    """
    router = APIRouter(prefix="/api")

    def call(user, permission, operation, *args):
        check_permission(user, permission)
        try:
            return operation(db_path, *args)
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except sqlite3.Error:
            raise HTTPException(503, "База тимчасово недоступна. Повтори спробу пізніше.") from None

    @router.get("/phrases")
    def phrases(q: str = Query(default="", max_length=200),
                offset: int = Query(default=0, ge=0, le=1000000),
                limit: int = Query(default=20, ge=1, le=100), user=Depends(current_user)):
        return call(user, "events.read", list_phrases, q, offset, limit)

    @router.get("/phrases/audio")
    def audio(fix_id: str = Query(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$"),
              user=Depends(current_user)):
        # Same TTL, deletion and authentication rules as the phrase itself.
        call(user, "events.read", load_edit_draft, fix_id)
        file_id = call(user, "events.read", audio_file_id, fix_id)
        try:
            content = download_voice(bot_token, file_id)
        except AudioUnavailable:
            raise HTTPException(502, "Не вдалося отримати аудіо з Telegram") from None
        return Response(content, media_type="audio/ogg", headers={"Cache-Control": "no-store"})

    return router
