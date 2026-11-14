"""Защищённый HTTP API Radio 345."""

import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware

from services.miniapp_auth import (
    TelegramAuthError,
    verify_init_data,
)
from services.user_store import require_permission
from services.miniapp_correction_editor import (
    CorrectionConflict,
    load_edit_draft,
    save_edit_draft,
)
from services.receiver_status import read_receiver_status
from services.receiver_control import (
    read_pause_state,
    set_pause_minutes,
    PauseVersionConflict,
)
from services.notify_mode_store import (
    load_notify_mode,
    save_notify_mode,
)
from api.management import management_router
from services.user_profiles import remember_verified_profile



class NotificationModeUpdate(BaseModel):
    mode: Literal["all", "watch", "commands", "off"]



class ReceiverControlUpdate(BaseModel):
    minutes: int = Field(ge=0, le=240, strict=True)
    expected_revision: int | None = Field(
        default=None, ge=0, strict=True
    )


class CorrectionDraftUpdate(BaseModel):
    fix_id: str = Field(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$")
    expected_text: str = Field(max_length=5000)
    corrected_text: str = Field(min_length=1, max_length=5000)


def create_app(db_path, bot_token):
    if not bot_token or not bot_token.strip():
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN")

    app = FastAPI(
        title="Radio 345 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://jackrv.github.io"],
        allow_credentials=False,
        allow_methods=["GET", "PUT"],
        allow_headers=["Authorization", "ngrok-skip-browser-warning"],
        max_age=300,
    )

    @app.middleware("http")
    async def prevent_private_data_caching(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request):
        auth = request.headers.get("authorization", "")
        prefix, separator, init_data = auth.partition(" ")

        if (
            prefix.lower() != "tma"
            or not separator
            or not init_data
            or init_data != init_data.strip()
        ):
            raise HTTPException(
                status_code=401,
                detail="Требуется авторизация Telegram",
            )

        try:
            verified_user = verify_init_data(
                init_data,
                bot_token,
                max_age=3600,
                return_user=True,
            )
            user_id = verified_user["id"]
        except TelegramAuthError:
            raise HTTPException(
                status_code=401,
                detail="Недействительная авторизация Telegram",
            ) from None

        try:
            role = require_permission(
                db_path,
                user_id,
                "status.read",
            )
            remember_verified_profile(db_path, verified_user)
        except PermissionError:
            raise HTTPException(
                status_code=403,
                detail="Доступ запрещён",
            ) from None
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="База пользователей недоступна",
            ) from None

        return {
            "user_id": user_id,
            "role": role,
        }

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/status")
    def status(user=Depends(current_user)):
        try:
            receiver = read_receiver_status(db_path)
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="Состояние приёмника недоступно",
            ) from None

        return {
            "api": "ready",
            "receiver": receiver,
        }

    def require_settings_permission(user, permission):
        try:
            require_permission(
                db_path, user["user_id"], permission
            )
        except PermissionError:
            raise HTTPException(
                status_code=403,
                detail="Недостаточно прав",
            ) from None
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="База пользователей недоступна",
            ) from None

    @app.get("/api/corrections/draft")
    def get_correction_draft(
        fix_id: str = Query(pattern=r"^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$"),
        user=Depends(current_user),
    ):
        require_settings_permission(user, "corrections.write")
        try:
            return load_edit_draft(db_path, fix_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Событие не найдено") from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="База недоступна") from None

    @app.put("/api/corrections/draft")
    def save_correction_draft(
        payload: CorrectionDraftUpdate,
        user=Depends(current_user),
    ):
        require_settings_permission(user, "corrections.write")
        try:
            return save_edit_draft(
                db_path,
                payload.fix_id,
                payload.expected_text,
                payload.corrected_text,
            )
        except CorrectionConflict:
            raise HTTPException(
                status_code=409,
                detail="Текст уже изменён. Открой редактор заново.",
            ) from None
        except LookupError:
            raise HTTPException(status_code=404, detail="Событие не найдено") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="Некорректный текст") from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="База недоступна") from None

    @app.get("/api/notifications/mode")
    def get_notification_mode(user=Depends(current_user)):
        require_settings_permission(user, "settings.read")

        try:
            mode = load_notify_mode(db_path)
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="Настройки недоступны",
            ) from None

        return {"mode": mode}

    @app.put("/api/notifications/mode")
    def update_notification_mode(
        payload: NotificationModeUpdate,
        user=Depends(current_user),
    ):
        require_settings_permission(user, "settings.write")

        try:
            save_notify_mode(db_path, payload.mode)
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="Не удалось сохранить настройки",
            ) from None

        return {"mode": payload.mode}

    @app.get("/api/receiver/control")
    def get_receiver_control(user=Depends(current_user)):
        try:
            return read_pause_state(db_path)
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="Управление приёмником недоступно",
            ) from None

    @app.put("/api/receiver/control")
    def update_receiver_control(
        payload: ReceiverControlUpdate,
        user=Depends(current_user),
    ):
        require_settings_permission(user, "audio.control")

        try:
            return set_pause_minutes(
                db_path,
                payload.minutes,
                expected_revision=payload.expected_revision,
            )
        except PauseVersionConflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Состояние приёмника изменилось. "
                    "Обновите данные перед повторной командой."
                ),
            ) from None
        except sqlite3.Error:
            raise HTTPException(
                status_code=503,
                detail="Не удалось сохранить команду",
            ) from None

    app.include_router(management_router(db_path, current_user, require_settings_permission, bot_token))
    return app
