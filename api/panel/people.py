"""Люди: користувачі й активність."""
from fastapi import Depends, HTTPException

from services import miniapp_management as store
from api.panel.models import UserUpdate

def register(app, ctx):
    db_path = ctx.db_path
    actor = ctx.actor

    @app.get("/api/users")
    def users_list(admin=Depends(actor)):
        """Користувачі з версіями для правки (ролі, доступ, назва, режим сповіщень)."""
        return store.list_users(db_path, admin)

    @app.put("/api/users/{user_id}")
    def user_update(user_id: int, payload: UserUpdate, admin=Depends(actor)):
        """Ті самі перевірки, що були в Mini App: версія, останній адміністратор, дозволені режими."""
        try:
            return store.write_user(db_path, admin, user_id, payload.role, payload.enabled,
                                    payload.expected_version, payload.display_name, payload.notify_mode)
        except store.ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/activity")
    def activity(user_id: int | None = None, admin=Depends(actor)):
        """Дії користувача з Mini App і бота (журнал з 16.11.2026) та його статистика."""
        from services import user_activity
        people = user_activity.users(db_path)
        if user_id is None and people:
            user_id = people[0]["user_id"]
        if user_id is not None and all(p["user_id"] != user_id for p in people):
            raise HTTPException(404, "Користувача не знайдено")
        return {"users": people, "selected": user_activity.overview(db_path, user_id) if user_id is not None else None}
