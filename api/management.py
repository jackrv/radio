"""Authenticated Mini App management routes; no runtime model imports."""

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import Response

from services import miniapp_management as store
from services.miniapp_correction_editor import load_edit_draft
from services.telegram_media_store import audio_file_id
from services.telegram_audio import download_voice, AudioUnavailable


class Entry(BaseModel):
    raw_line: str = Field(min_length=1, max_length=500)
    enabled: bool = Field(strict=True)


class DictionaryUpdate(BaseModel):
    entries: list[Entry] = Field(max_length=1000)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleUpdate(BaseModel):
    disabled: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class UserUpdate(BaseModel):
    role: Literal["admin", "editor", "viewer"]
    enabled: bool = Field(strict=True)
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RecognitionValues(BaseModel):
    USE_LLM: bool = Field(strict=True)
    VAD_LEVEL: int = Field(ge=0, le=3, strict=True)
    SILENCE_END_MS: int = Field(ge=150, le=3000, strict=True)
    MIN_SPEECH_MS: int = Field(ge=90, le=3000, strict=True)
    MAX_SEGMENT_S: int = Field(ge=3, le=60, strict=True)

    @model_validator(mode="after")
    def valid_duration(self):
        if self.MIN_SPEECH_MS >= self.MAX_SEGMENT_S * 1000:
            raise ValueError("Минимальная речь должна быть короче максимального сегмента")
        return self


class SettingsUpdate(BaseModel):
    values: RecognitionValues
    expected_revision: int = Field(ge=0, strict=True)


def management_router(db_path, current_user, check_permission, bot_token):
    router = APIRouter(prefix="/api")

    def call(user, permission, operation, *args):
        check_permission(user, permission)
        try:
            return operation(db_path, *args)
        except store.ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостаточно прав") from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except sqlite3.Error:
            raise HTTPException(503, "База временно недоступна. Повтори попытку позже.") from None

    @router.get("/overview")
    def overview(user=Depends(current_user)):
        return call(user, "events.read", store.overview)

    @router.get("/phrases")
    def phrases(q: str = Query(default="", max_length=200),
                offset: int = Query(default=0, ge=0, le=1000000),
                limit: int = Query(default=20, ge=1, le=100), user=Depends(current_user)):
        return call(user, "events.read", store.list_phrases, q, offset, limit)

    @router.get("/dictionaries/{kind}")
    def dictionary(kind: Literal["callsigns", "commands", "telegram"], user=Depends(current_user)):
        return call(user, "dictionaries.read", store.read_dictionary, kind)

    @router.get("/phrases/audio")
    def audio(fix_id: str = Query(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$"),
              user=Depends(current_user)):
        # Same TTL, deletion and authentication rules as the phrase itself.
        call(user, "events.read", load_edit_draft, fix_id)
        file_id = call(user, "events.read", audio_file_id, fix_id)
        try:
            content = download_voice(bot_token, file_id)
        except AudioUnavailable:
            raise HTTPException(502, "Не удалось получить аудио из Telegram") from None
        return Response(content, media_type="audio/ogg", headers={"Cache-Control": "no-store"})

    @router.put("/dictionaries/{kind}")
    def update_dictionary(kind: Literal["callsigns", "commands", "telegram"],
                          payload: DictionaryUpdate, user=Depends(current_user)):
        return call(user, "dictionaries.write", store.write_dictionary, user["user_id"],
                    kind, [e.model_dump() for e in payload.entries], payload.expected_version)

    @router.get("/word-rules")
    def rules(q: str = Query(default="", max_length=200),
              offset: int = Query(default=0, ge=0, le=1000000),
              limit: int = Query(default=20, ge=1, le=100), user=Depends(current_user)):
        return call(user, "dictionaries.read", store.list_rules, q, offset, limit)

    @router.put("/word-rules/{rule_id}")
    def update_rule(rule_id: int, payload: RuleUpdate, user=Depends(current_user)):
        return call(user, "dictionaries.write", store.toggle_rule, user["user_id"], rule_id,
                    payload.disabled, payload.expected_version)

    @router.get("/users")
    def users(user=Depends(current_user)):
        return call(user, "users.manage", store.list_users, user["user_id"])

    @router.put("/users/{user_id}")
    def update_user(user_id: int, payload: UserUpdate, user=Depends(current_user)):
        return call(user, "users.manage", store.write_user, user["user_id"], user_id,
                    payload.role, payload.enabled, payload.expected_version)

    @router.get("/recognition/settings")
    def settings(user=Depends(current_user)):
        return call(user, "settings.read", store.read_settings)

    @router.put("/recognition/settings")
    def update_settings(payload: SettingsUpdate, user=Depends(current_user)):
        return call(user, "settings.write", store.write_settings, user["user_id"],
                    payload.values.model_dump(), payload.expected_revision)

    return router
