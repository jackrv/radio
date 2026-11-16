"""Authenticated Mini App management routes; no runtime model imports."""

import sqlite3
import subprocess

import config
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from fastapi.responses import Response

from services import miniapp_management as store
from services.miniapp_correction_editor import load_edit_draft
from services.telegram_media_store import audio_file_id
from services.user_activity import record as record_activity
from services.telegram_audio import download_voice, AudioUnavailable


class Entry(BaseModel):
    raw_line: str = Field(min_length=1, max_length=500)
    enabled: bool = Field(strict=True)


class DictionaryUpdate(BaseModel):
    entries: list[Entry] = Field(max_length=store.MAX_DICTIONARY_ENTRIES)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleApproval(BaseModel):
    approved: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleUpdate(BaseModel):
    disabled: bool = Field(strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class NotificationSampleUpdate(BaseModel):
    fix_id: str = Field(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$")
    confirmed_isolated: bool = Field(strict=True)


class UserUpdate(BaseModel):
    role: Literal["admin", "editor", "viewer"]
    enabled: bool = Field(strict=True)
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    display_name: str | None = Field(default=None, max_length=80)
    notify_mode: Literal["all", "watch", "commands", "off"] | None = None


class RecognitionValues(BaseModel):
    USE_LLM: bool = Field(strict=True)
    VAD_LEVEL: int = Field(ge=0, le=3, strict=True)
    SILENCE_END_MS: int = Field(ge=150, le=3000, strict=True)
    MIN_SPEECH_MS: int = Field(ge=90, le=3000, strict=True)
    MAX_SEGMENT_S: int = Field(ge=3, le=60, strict=True)

    @model_validator(mode="after")
    def valid_duration(self):
        if self.MIN_SPEECH_MS >= self.MAX_SEGMENT_S * 1000:
            raise ValueError("Мінімальна тривалість мовлення має бути меншою за максимальний сегмент")
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
            raise HTTPException(403, "Недостатньо прав") from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except sqlite3.Error:
            raise HTTPException(503, "База тимчасово недоступна. Повтори спробу пізніше.") from None

    @router.get("/overview")
    def overview(user=Depends(current_user)):
        result = call(user, "events.read", store.overview)
        # Автоматичні зміни Qwen — окремо від правок оператора й правил (поточний запуск).
        result["automatic"] = store.automatic_changes(config.BASE_DIR / "diagnostics.json")
        return result

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
            raise HTTPException(502, "Не вдалося отримати аудіо з Telegram") from None
        return Response(content, media_type="audio/ogg", headers={"Cache-Control": "no-store"})

    @router.put("/notification-sounds/from-phrase")
    def notification_sample(payload: NotificationSampleUpdate, user=Depends(current_user)):
        check_permission(user, "settings.write")
        if not payload.confirmed_isolated:
            raise HTTPException(422, "Підтвердь відсутність мовлення у зразку")
        call(user, "events.read", load_edit_draft, payload.fix_id)
        file_id = call(user, "events.read", audio_file_id, payload.fix_id)
        from services.notification_samples import add_sample, ogg_to_wav
        try:
            content = ogg_to_wav(download_voice(bot_token, file_id))
            result = add_sample(config.NOTIFICATION_SOUNDS_DIR, content, confirmed_isolated=True)
            record_activity(db_path, user["user_id"], "sample", payload.fix_id, {})
            return result
        except AudioUnavailable:
            raise HTTPException(502,"Не вдалося отримати аудіо") from None
        except (subprocess.SubprocessError, FileNotFoundError):
            raise HTTPException(502,"Не вдалося перетворити коротке аудіо") from None
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        except OSError:
            raise HTTPException(503,"Каталог зразків недоступний") from None

    @router.put("/dictionaries/{kind}")
    def update_dictionary(kind: Literal["callsigns", "commands", "telegram"],
                          payload: DictionaryUpdate, user=Depends(current_user)):
        result = call(user, "dictionaries.write", store.write_dictionary, user["user_id"],
                      kind, [e.model_dump() for e in payload.entries], payload.expected_version)
        record_activity(db_path, user["user_id"], "dictionary", kind,
                        {"lines": sum(e.enabled for e in payload.entries)})
        return result

    @router.get("/word-rules")
    def rules(q: str = Query(default="", max_length=200),
              offset: int = Query(default=0, ge=0, le=1000000),
              limit: int = Query(default=20, ge=1, le=100), user=Depends(current_user)):
        return call(user, "dictionaries.read", store.list_rules, q, offset, limit)

    @router.put("/word-rules/{rule_id}")
    def update_rule(rule_id: int, payload: RuleUpdate, user=Depends(current_user)):
        result = call(user, "dictionaries.write", store.toggle_rule, user["user_id"], rule_id,
                      payload.disabled, payload.expected_version)
        record_activity(db_path, user["user_id"], "rule", str(rule_id), {"disabled": payload.disabled})
        return result

    @router.put("/word-rules/{rule_id}/approval")
    def approve_rule(rule_id: int, payload: RuleApproval, user=Depends(current_user)):
        result = call(user, "dictionaries.write", store.approve_rule, user["user_id"], rule_id,
                      payload.approved, payload.expected_version)
        record_activity(db_path, user["user_id"], "rule_approval", str(rule_id), {"approved": payload.approved})
        return result

    @router.get("/users")
    def users(user=Depends(current_user)):
        return call(user, "users.manage", store.list_users, user["user_id"])

    @router.put("/users/{user_id}")
    def update_user(user_id: int, payload: UserUpdate, user=Depends(current_user)):
        result = call(user, "users.manage", store.write_user, user["user_id"], user_id,
                      payload.role, payload.enabled, payload.expected_version, payload.display_name,
                      payload.notify_mode)
        record_activity(db_path, user["user_id"], "user", str(user_id),
                        {"role": payload.role, "enabled": payload.enabled, "notify_mode": payload.notify_mode})
        return result

    @router.get("/recognition/settings")
    def settings(user=Depends(current_user)):
        return call(user, "settings.read", store.read_settings)

    @router.put("/recognition/settings")
    def update_settings(payload: SettingsUpdate, user=Depends(current_user)):
        result = call(user, "settings.write", store.write_settings, user["user_id"],
                      payload.values.model_dump(), payload.expected_revision)
        record_activity(db_path, user["user_id"], "settings", "", {})
        return result

    return router
