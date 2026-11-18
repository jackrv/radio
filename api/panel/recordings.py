"""Аудіоархів: список, розмітка, перевірка тексту, видалення в Telegram, утримання мікрофона."""
import json
import logging
import re

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from services.notification_samples import add_sample
from services.sqlite_connection import get_db
from api.panel.models import RecordingRestore, RecordingReview, RoleSuggestion, TextCheck, ListeningHold, SampleConfirmation

_RANGE = re.compile(r"bytes=(\d*)-(\d*)\Z")


def ranged_audio(data, header):
    """WAV із підтримкою Range: без неї Chrome не перемотує запис, що вже грає (скидає на початок)."""
    size = len(data)
    match = _RANGE.fullmatch((header or "").strip())
    if not match or not (match[1] or match[2]):
        return Response(data, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})
    if match[1]:
        start, end = int(match[1]), min(size - 1, int(match[2])) if match[2] else size - 1
    else:   # «bytes=-N» — останні N байтів
        start, end = max(0, size - int(match[2])), size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    return Response(data[start:end + 1], status_code=206, media_type="audio/wav",
                    headers={"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})


def register(app, ctx):
    db_path = ctx.db_path
    recordings = ctx.recordings
    actor = ctx.actor
    samples_dir = ctx.samples_dir

    @app.post("/api/recordings/suggest-roles")
    def suggest_roles(payload: RoleSuggestion, admin=Depends(actor)):
        """Позивні зі словника, знайдені в тексті оператора, — для швидкого вибору «кому / від».

        Для двох різних позивних пропонуються й ролі (перший — кому, другий — від):
        оператор вирішив, що зручність розмітки важливіша за незалежну оцінку ролей.
        """
        from core.analyzer import fmt
        from services.text_check import dictionaries_from_db
        with get_db(db_path) as db:
            callsigns, _, _ = dictionaries_from_db(db)
        if callsigns is None:
            return {"callsigns": []}
        found = []
        items = callsigns.find(payload.text)
        for item in items:
            text = fmt([item])
            if text not in found:
                found.append(text)
        # Ролі підставляються за тим самим правилом, що й у приймачі, лише для однієї передачі:
        # «я Х» — Х одразу «Від» (скільки б позивних не було), єдиний позивний перед «я» — «Кому»;
        # без «я» — лише для двох різних позивних. Інакше оператор вирішує сам.
        roles = None
        from services.text_check import has_several_transmissions
        if items and not has_several_transmissions(payload.text):
            from core.analyzer import _SELF, split_roles
            to_found, from_found = split_roles(payload.text, callsigns)
            if _SELF.search(payload.text) and len(from_found) == 1:
                roles = {"recipient": fmt(to_found) if len(to_found) == 1 else None, "sender": fmt(from_found)}
            elif len(items) == 2 and len(to_found) == 1 and len(from_found) == 1:
                roles = {"recipient": fmt(to_found), "sender": fmt(from_found)}
            elif len({fmt([i]) for i in items}) == 1:
                # Один позивний і сталий зворот (за розміткою оператора 18.11.2026):
                # «Клен 15, посилку отримав» — говорить сам Клен 15 → «від» (17 із 17 збігів);
                # «Клен 15, прийнято, прийом» — відповідають Клену 15 → «кому».
                from core.text_metrics import metric_words
                words, name = " ".join(metric_words(payload.text)), fmt(items[:1])
                if "посилку отримав" in words:
                    roles = {"recipient": None, "sender": name}
                elif re.search(r"(?<!\w)прийнято(?!\w)", words):
                    roles = {"recipient": name, "sender": None}
        return {"callsigns": found, "roles": roles}

    @app.post("/api/text-check")
    def text_check(payload: TextCheck, admin=Depends(actor)):
        """Зауваження до оформлення тексту еталона; нічого не змінює."""
        from services import daily_codes
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            codes = daily_codes.read(db, daily_codes.local_day(payload.at))
            return check_with_db(db, payload.text, codes)

    @app.get("/api/recordings")
    def recording_list(request: Request, offset: int = 0, limit: int = 25,
                       q: str = "", edits: str = "", view: str = "all", since: float | None = None,
                       until: float | None = None, order: str = "new", admin=Depends(actor)):
        if (set(request.query_params) - {"offset", "limit", "q", "edits", "view", "since", "until", "order"} or order not in ("new", "doubtful")
                or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100 or len(q) > 200):
            raise HTTPException(422, "Некоректні параметри")
        if not re.fullmatch(r"(?:|miniapp|[1-9][0-9]{0,17})", edits):
            raise HTTPException(422, "Некоректні параметри")
        from services.recordings import VIEWS
        view = {"recent": "reviewed"}.get(view, view)   # старі посилання на «Нещодавно розмічені»
        if (view not in VIEWS) or any(v is not None and not 0 <= v <= 4e9 for v in (since, until)):
            raise HTTPException(422, "Некоректні параметри")
        from services import user_activity
        selected = None
        if edits == "miniapp":
            selected = "miniapp"
        elif edits:
            selected = list(user_activity.corrections(db_path, user_id=int(edits)))
        page = recordings.listing(offset, limit, search=q, edits=selected, view=view, since=since, until=until, order=order)
        page["views"] = recordings.view_counts(since, until)
        page["confidence_low"] = recordings.confidence_threshold()   # нижче — «сумнівно» (найменш упевнені 25 %)
        # Хто й коли правив запис у Mini App (журнал дій з 16.11.2026).
        linked = [item["data"].get("fix_id") for item in page["items"] if item["data"].get("fix_id")]
        if linked:
            found = user_activity.corrections(db_path, fix_ids=linked)
            for item in page["items"]:
                if item["data"].get("fix_id") in found:
                    item["corrections"] = found[item["data"]["fix_id"]]
        page["editors"] = user_activity.editors(db_path)
        # Зауваження до оформлення еталонів — щоб картки з ними було видно одразу.
        from services import daily_codes
        from services.text_check import check_with_db
        with get_db(db_path) as db:
            codes_by_day = {}
            for item in page["items"]:
                if item.get("reference") and item.get("label") in ("speech", "unclear"):
                    day = daily_codes.local_day(item["created"])
                    if day not in codes_by_day:
                        codes_by_day[day] = daily_codes.read(db, day)
                    item["text_check"] = check_with_db(db, item["reference"], codes_by_day[day])
        # Скільки копій фрази лишилося в чатах і чи ще можна видалити їх у всіх.
        from services import telegram_recall
        sent = telegram_recall.state(db_path, linked)
        for item in page["items"]:
            fix_id = item["data"].get("fix_id")
            if fix_id in sent:
                item["telegram"] = {**sent[fix_id], "deletable": telegram_recall.deletable(item["created"]),
                                    "deletable_until": item["created"] + telegram_recall.DELETE_WINDOW_S - telegram_recall.SAFETY_S}
        return page

    @app.post("/api/recordings/{record_id}/telegram-delete")
    def recording_telegram_delete(record_id: str, admin=Depends(actor)):
        """Видалити надіслану фразу в Telegram в усіх отримувачів (протягом 48 год після надсилання)."""
        from services import telegram_recall, user_activity
        from services.telegram_media_store import FIX_RE
        try:
            record = recordings.get(record_id)
        except (LookupError, ValueError):
            raise HTTPException(404, "Запис не знайдено") from None
        fix_id = (record.get("data") or {}).get("fix_id") or ""
        if not FIX_RE.fullmatch(fix_id):
            raise HTTPException(422, "Цей запис не надсилався в Telegram")
        if not telegram_recall.deletable(record["created"]):
            raise HTTPException(422, "Telegram дозволяє ботові видаляти повідомлення лише протягом 48 годин")
        count = telegram_recall.request_delete(db_path, fix_id)
        user_activity.record(db_path, admin, "recall", fix_id, {"copies": count})
        return {**telegram_recall.state(db_path, [fix_id])[fix_id], "queued": count}

    @app.get("/api/recordings/summary")
    def recording_summary(admin=Depends(actor)):
        return recordings.summary()

    @app.get("/api/recordings/export")
    def recording_export(admin=Depends(actor)):
        with recordings.connect() as db:
            rows = db.execute("SELECT * FROM recordings ORDER BY created").fetchall()
        return Response(json.dumps({"schema": 1, "items": [recordings.decode(row) for row in rows]}, ensure_ascii=False),
                        media_type="application/json", headers={"Content-Disposition": 'attachment; filename="recordings.json"'})

    @app.get("/api/recordings/{record_id}/audio")
    def recording_audio(record_id: str, request: Request, admin=Depends(actor)):
        try:
            data = recordings.audio(record_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return ranged_audio(data, request.headers.get("range"))

    @app.put("/api/recordings/{record_id}/review")
    def recording_review(record_id: str, payload: RecordingReview, admin=Depends(actor)):
        if payload.label == "speech" and not payload.reference.strip():
            raise HTTPException(422, "Введіть почутий текст або позначте нерозбірливий запис")
        if payload.label in ("noise", "notification", "room") and payload.reference.strip():
            raise HTTPException(422, "Для запису без мовлення залиште текст порожнім")
        role_fields = {name: getattr(payload, name) for name in ("reference_recipient", "reference_sender", "multiple_messages")
                       if name in payload.model_fields_set}
        if payload.label in ("noise", "notification", "room") and any(
                isinstance(value, str) and value.strip() for value in role_fields.values()):
            raise HTTPException(422, "Для запису без мовлення позивні не вказуються")
        try:
            result = recordings.review(record_id, payload.reference.strip(), payload.label, payload.expected_revision, **role_fields)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        result["event_sync"] = sync_event_from_reference(result, admin)
        return result

    @app.post("/api/recordings/{record_id}/restore")
    def recording_restore(record_id: str, payload: RecordingRestore, admin=Depends(actor)):
        """«↶ Скасувати» швидку розмітку: повернути запис у стан до неї (у т. ч. нерозмічений)."""
        if (payload.reference is None) != (payload.label is None):
            raise HTTPException(422, "Некоректний попередній стан")
        try:
            result = recordings.restore(record_id, payload.expected_revision, payload.model_dump(exclude={"expected_revision"}))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        result["event_sync"] = sync_event_from_reference(result, admin)   # повернутий еталон мовлення — знову текст події
        return result

    def sync_event_from_reference(record, admin):
        """Еталон мовлення стає текстом пов'язаної події: оновлюються журнал, підпис у
        Telegram і навчання правил — так само, як після правки в Mini App.

        Повертає None (немає події), "same", "updated", "hidden" або "failed".
        Збій не скасовує збережений еталон.
        """
        fix_id = (record.get("data") or {}).get("fix_id") or ""
        reference = (record.get("reference") or "").strip()
        if record.get("label") not in ("speech", "unclear") or not reference or not re.fullmatch(r"db:[1-9][0-9]{0,17}", fix_id):
            return None
        from services.miniapp_correction_editor import CorrectionConflict, save_edit_draft
        from services.telegram_media_store import is_hidden
        try:
            if is_hidden(db_path, fix_id):
                return "hidden"
            for _ in range(2):   # подію могли змінити між читанням і записом — одна повторна спроба
                with get_db(db_path) as db:
                    row = db.execute("SELECT text FROM radio_events WHERE id=?", (int(fix_id[3:]),)).fetchone()
                if row is None:
                    return None
                if row[0] == reference:
                    return "same"
                try:
                    save_edit_draft(db_path, fix_id, row[0], reference, actor_id=admin)
                    return "updated"
                except CorrectionConflict:
                    continue
            return "failed"
        except Exception:
            logging.getLogger("radio").exception("Не вдалося перенести еталон у подію %s", fix_id)
            return "failed"

    @app.post("/api/recordings/{record_id}/notification-sample")
    def recording_sample(record_id: str, payload: SampleConfirmation, admin=Depends(actor)):
        """Чистий звук сповіщення з архіву стає зразком спектрального фільтра (після перезапуску)."""
        if not payload.confirmed_isolated:
            raise HTTPException(422, "Підтвердь, що у фрагменті немає мовлення")
        try:
            content = recordings.audio(record_id)
        except (LookupError, ValueError):
            raise HTTPException(404, "Аудіо запису недоступне") from None
        try:
            return add_sample(samples_dir, content, confirmed_isolated=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/api/listening-hold")
    def listening_hold(payload: ListeningHold, admin=Depends(actor)):
        """Поки в панелі звучить запис, приймач не слухає мікрофон (без власної луни)."""
        from services.receiver_control import set_listening_hold
        try:
            return set_listening_hold(db_path, payload.seconds)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    ctx.sync_event_from_reference = sync_event_from_reference
