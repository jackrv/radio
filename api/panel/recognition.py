"""Розпізнавання: словники, коди доби, фільтри тексту, навчання, налаштування."""
from api.panel.jobs import start_task
from fastapi import Depends, HTTPException

from services.management.common import ManagementConflict
from services.management.learning import approve_rule, learning_queue, toggle_rule
from services.management.settings import write_settings
from services.receiver_status import read_receiver_status
from services.sqlite_connection import get_db
from api.panel.models import DailyCodesUpdate, RuntimeSettingsUpdate, TextListCheck, TextListUpdate, DictionaryUpdate, RuleApproval, RuleUpdate, SettingsUpdate
from api.panel.common import read_diagnostics

def register(app, ctx):
    db_path = ctx.db_path
    recordings = ctx.recordings
    actor = ctx.actor
    diagnostics_path = ctx.diagnostics_path

    @app.post('/api/learning/archive',status_code=202)
    def learn_archive(admin=Depends(actor)):
        return start_task(ctx,'archive_learning',actor_id=admin)

    @app.get("/api/daily-codes")
    def daily_codes_list(admin=Depends(actor)):
        """Коди доби, внесені вручну, за останні дні."""
        from services import daily_codes
        today = daily_codes.local_day()
        with get_db(db_path) as db:
            permanent = daily_codes.read_permanent(db)
        return {"today": today, "today_period": daily_codes.period(today), "permanent": permanent,
                "change_hour": daily_codes.change_hour(), "items": daily_codes.recent(db_path)}

    @app.put("/api/daily-codes/permanent")
    def daily_codes_permanent(payload: DailyCodesUpdate, admin=Depends(actor)):
        """Постійні коди: діють у будь-яку добу разом із кодами доби."""
        from services import daily_codes
        try:
            return {"codes": daily_codes.write_permanent(db_path, payload.codes)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/daily-codes/{day}")
    def daily_codes_update(day: str, payload: DailyCodesUpdate, admin=Depends(actor)):
        from services import daily_codes
        try:
            codes = daily_codes.write(db_path, day, payload.codes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"day": day, "codes": codes}

    @app.get("/api/runtime-settings")
    def runtime_settings(admin=Depends(actor)):
        from services.runtime_settings import read_runtime_settings
        result = read_runtime_settings(db_path)
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {}
        gauges = diagnostics.get("gauges") or {}
        result["applied"] = {"revision": gauges.get("runtime_settings.revision"),
                             "receiver": read_receiver_status(db_path)}
        return result

    @app.put("/api/runtime-settings")
    def runtime_settings_update(payload: RuntimeSettingsUpdate, admin=Depends(actor)):
        from services.runtime_settings import RuntimeSettingsConflict, write_runtime_settings
        try:
            return write_runtime_settings(db_path, payload.values, payload.expected_revision)
        except RuntimeSettingsConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/text-lists")
    def text_lists(admin=Depends(actor)):
        from services.text_lists import read_lists
        result = read_lists(db_path)
        try:
            diagnostics = read_diagnostics(diagnostics_path)
        except ValueError:
            diagnostics = {}
        gauges = diagnostics.get("gauges") or {}
        result["applied"] = {"revision": gauges.get("text_lists.revision"),
                             "updated_at": diagnostics.get("updated_at"), "receiver": read_receiver_status(db_path)}
        return result

    @app.get("/api/text-lists/callsign-alias-candidates")
    def callsign_alias_candidates(admin=Depends(actor)):
        """Підказка: почуті Whisper варіанти позивних із розмічених записів. Нічого не записує."""
        from core.callsigns import Callsigns
        from services.alias_candidates import collect
        from services.text_lists import _dictionary_names, ensure_tables, load_active, sync_aliases
        with get_db(db_path) as db:
            ensure_tables(db)
            sync_aliases(db)
            _, active = load_active(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone():
                return {"items": [], "records": 0}
            lines = [line for (line,) in db.execute("SELECT raw_line FROM dictionary_entries "
                     "WHERE enabled=1 AND dictionary_type='callsigns' ORDER BY line_number")]
            all_names, _ = _dictionary_names(db)
        callsigns = Callsigns.from_lines(lines)
        records = recordings.labelled_speech()
        return {"items": collect(records, callsigns, all_names, active["callsign_aliases"]), "records": len(records)}

    @app.get("/api/text-lists/noise-candidates")
    def noise_candidates(admin=Depends(actor)):
        """Підказка: тексти Whisper, що повторюються на записах без мовлення. Нічого не записує."""
        from services.noise_candidates import collect, key
        from services.text_lists import _dictionary_names, ensure_tables, load_active
        with get_db(db_path) as db:
            ensure_tables(db)
            _, active = load_active(db)
            names, _ = _dictionary_names(db) if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dictionary_entries'").fetchone() else (set(), None)
        keys = {key(v) for v in (*active["blacklist"], *active["short_garbage"])}
        records = recordings.labelled_texts()
        return {"items": collect(records, keys, set(names)), "records": len(records)}

    @app.put("/api/text-lists/{list_type}/check")
    def text_list_check(list_type: str, payload: TextListCheck, admin=Depends(actor)):
        from services.text_lists import check_list
        try:
            return check_list(db_path, list_type, [e.model_dump() for e in payload.entries])
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.put("/api/dictionaries/{kind}/check")
    def dictionary_check(kind: str, payload: TextListCheck, admin=Depends(actor)):
        """Перевірка рядків словника без збереження: помилка, яку покаже «Зберегти»."""
        from services.management.dictionaries import validate_dictionary
        if kind not in ("callsigns", "commands", "telegram"):
            raise HTTPException(404, "Невідомий словник")
        try:
            validate_dictionary([e.model_dump() for e in payload.entries], kind)
        except ValueError as exc:
            return {"errors": [str(exc)], "warnings": []}
        return {"errors": [], "warnings": []}

    @app.put("/api/text-lists/{list_type}/evaluate", status_code=202)
    def text_list_evaluate(list_type: str, payload: TextListCheck, admin=Depends(actor)):
        """Як змінився б текст на розмічених записах із цим (незбереженим) списком.

        Повтор текстової обробки на збережених виходах Whisper — без моделей, приймач працює.
        """
        from services import panel_jobs
        if list_type not in panel_jobs.LISTS:
            raise HTTPException(404, "Цей список не перевіряється на еталонах")
        try:
            return start_task(ctx, "evaluate", list_type=list_type,
                                    entries=[e.model_dump() for e in payload.entries])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/text-lists/{list_type}")
    def text_list_update(list_type: str, payload: TextListUpdate, admin=Depends(actor)):
        from services.text_lists import LIST_TYPES, TextListConflict, write_list
        if list_type not in LIST_TYPES:
            raise HTTPException(404, "Невідомий список")
        try:
            return write_list(db_path, list_type, [e.model_dump() for e in payload.entries], payload.expected_revision)
        except TextListConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/dictionaries")
    def dictionaries(admin=Depends(actor)):
        """Словники позивних, команд і Telegram — ті самі, що в Mini App."""
        from services.management.dictionaries import read_dictionary
        return {kind: read_dictionary(db_path, kind) for kind in ("callsigns", "commands", "telegram")}

    @app.put("/api/dictionaries/{kind}")
    def dictionary_update(kind: str, payload: DictionaryUpdate, admin=Depends(actor)):
        """Збереження з тією самою перевіркою рядків і захистом від одночасної правки, що й у Mini App."""
        from services.management.common import ManagementConflict
        from services.management.dictionaries import write_dictionary
        if kind not in ("callsigns", "commands", "telegram"):
            raise HTTPException(404, "Невідомий словник")
        try:
            return write_dictionary(db_path, admin, kind, [e.model_dump() for e in payload.entries],
                                    payload.expected_version)
        except ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/learning")
    def learning(admin=Depends(actor)):
        """Навчені заміни слів: що вже діє в розпізнаванні і чого бракує решті."""
        return learning_queue(db_path)

    def _rule_call(function, admin, rule_id, *args):
        try:
            return function(db_path, admin, rule_id, *args)
        except ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except PermissionError:
            raise HTTPException(403, "Недостатньо прав") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/learning/{rule_id}")
    def learning_toggle(rule_id: int, payload: RuleUpdate, admin=Depends(actor)):
        return _rule_call(toggle_rule, admin, rule_id, payload.disabled, payload.expected_version)

    @app.put("/api/learning/{rule_id}/approval")
    def learning_approval(rule_id: int, payload: RuleApproval, admin=Depends(actor)):
        return _rule_call(approve_rule, admin, rule_id, payload.approved, payload.expected_version)

    @app.put("/api/settings")
    def settings(payload: SettingsUpdate, admin=Depends(actor)):
        try:
            return write_settings(db_path,admin,payload.values.model_dump(),payload.expected_revision)
        except ManagementConflict as exc:
            raise HTTPException(409,str(exc)) from None
