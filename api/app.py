"""Защищённый HTTP API Radio 345."""

import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from services.miniapp_auth import (
    TelegramAuthError,
    verify_init_data,
)
from services.user_store import require_permission
from services.receiver_status import read_receiver_status


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
        allow_methods=["GET"],
        allow_headers=["Authorization", "ngrok-skip-browser-warning"],
        max_age=300,
    )

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
            user_id = verify_init_data(
                init_data,
                bot_token,
                max_age=3600,
            )
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

    return app
