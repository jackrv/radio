"""Видалення надісланої фрази в Telegram в усіх, кому вона надійшла (з «Аудіоархіву» панелі).

Панель лише ставить копії в чергу (telegram_deliveries.delete_requested) і одразу
приховує фразу в журналі й Mini App — як «Приховати для всіх» адміністратора.
Самі deleteMessage робить фоновий цикл бота (maintenance_once), тож запит не губиться,
навіть якщо приймач зупинено: копії видаляться після запуску.

Telegram дозволяє ботові видаляти власні повідомлення лише протягом 48 годин.
"""
import time

import requests

from shared.diagnostics import runtime
from shared.sqlite_connection import get_db
from shared.telegram_media_store import deletion_result, is_hidden, ensure_table as ensure_media
from shared.telegram_sync import ensure_tables

DELETE_WINDOW_S = 48 * 3600
SAFETY_S = 15 * 60          # запас: копії надсилаються кількома секундами пізніше запису, черга може чекати
MAX_ATTEMPTS = 8
GONE = ("Bad Request: message to delete not found",)
TOO_OLD = ("Bad Request: message can't be deleted", "Bad Request: message can't be deleted for everyone")


def deletable(created, now=None):
    """Чи ще можна видалити фразу, надіслану в момент запису ``created``."""
    now = time.time() if now is None else now
    return now - created < DELETE_WINDOW_S - SAFETY_S


def state(db_path, fix_ids):
    """{fix_id: {"copies": живих копій, "pending": у черзі на видалення, "hidden": фразу приховано}}."""
    fix_ids = [f for f in dict.fromkeys(fix_ids) if f]
    if not fix_ids:
        return {}
    with get_db(db_path) as db:
        ensure_tables(db)
        marks = ",".join("?" * len(fix_ids))
        rows = db.execute(f"""SELECT fix_id, SUM(unavailable=0),
            SUM(unavailable=0 AND delete_requested IS NOT NULL AND attempts<{MAX_ATTEMPTS}),
            SUM(delete_requested IS NOT NULL),
            SUM(unavailable=0 AND delete_requested IS NOT NULL AND attempts>={MAX_ATTEMPTS})
            FROM telegram_deliveries WHERE fix_id IN ({marks}) GROUP BY fix_id""",
                          fix_ids).fetchall()
        found = {f: {"copies": live or 0, "pending": pending or 0, "requested": bool(asked), "exhausted": exhausted or 0}
                 for f, live, pending, asked, exhausted in rows}
        for fix_id in fix_ids:
            found.setdefault(fix_id, {"copies": 0, "pending": 0, "requested": False, "exhausted": 0})
            found[fix_id]["hidden"] = is_hidden(db_path, fix_id, db=db)
    return found


def request_delete(db_path, fix_id, *, now=None, retry_exhausted=True):
    """Поставити всі живі копії фрази в чергу на видалення. Повертає кількість копій."""
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_tables(db)
        ensure_media(db)
        # Давні доставки могли зберігатися лише тут. Перенести адресу до
        # tombstone: пізніша migrate_legacy бере тільки visible-рядки.
        db.execute('''INSERT OR IGNORE INTO telegram_deliveries(fix_id,chat_id,message_id,kind)
            SELECT fix_id,CAST(chat_id AS INTEGER),message_id,
                CASE WHEN voice_file_id IS NULL THEN 'text' ELSE 'voice' END
            FROM telegram_phrase_messages WHERE fix_id=? AND state='visible'
                AND CAST(chat_id AS INTEGER)>0 AND message_id IS NOT NULL''', (fix_id,))
        db.execute("""INSERT INTO telegram_phrase_messages(fix_id,state) VALUES (?, 'deleting')
            ON CONFLICT(fix_id) DO UPDATE SET state='deleting'""", (fix_id,))
        count = db.execute("""UPDATE telegram_deliveries SET delete_requested=?, attempts=0, next_try=0
            WHERE fix_id=? AND unavailable=0 AND (delete_requested IS NULL OR (? AND attempts>=?))""",
                           (now, fix_id, int(retry_exhausted), MAX_ATTEMPTS)).rowcount
    # Фраза зникає з журналу й Mini App одразу; копії в чатах — щойно бот їх видалить.
    if not count and not _left(db_path, fix_id):
        deletion_result(db_path, fix_id, True)
    return count


def _left(db_path, fix_id):
    with get_db(db_path) as db:
        return db.execute("""SELECT COUNT(*) FROM telegram_deliveries
            WHERE fix_id=? AND unavailable=0 AND delete_requested IS NOT NULL""",
                          (fix_id,)).fetchone()[0]


def process_deletions(service, *, limit=5, now=None):
    """Крок фонового циклу бота: видалити до ``limit`` копій із черги."""
    now = time.time() if now is None else now
    with get_db(service.db_path) as db:
        ensure_tables(db)
        jobs = db.execute("""SELECT fix_id, chat_id, message_id FROM telegram_deliveries
            WHERE delete_requested IS NOT NULL AND unavailable=0 AND next_try<=? AND attempts<?
            ORDER BY delete_requested, rowid LIMIT ?""", (now, MAX_ATTEMPTS, limit)).fetchall()
    done = set()
    for fix_id, chat, message in jobs:
        stop = getattr(service, "_maintenance_stop", None)
        if stop is not None and stop.is_set():
            break
        with get_db(service.db_path) as db:
            db.execute("""UPDATE telegram_deliveries SET attempts=attempts+1, next_try=?
                WHERE fix_id=? AND chat_id=? AND message_id=?""", (now + 30, fix_id, chat, message))
        try:
            response = service._request("post", "deleteMessage", data={"chat_id": chat, "message_id": message},
                                        timeout=(5, 10))
            result = response.json()
            if not isinstance(result, dict):
                result = {}
        except (requests.RequestException, ValueError, AttributeError):
            runtime.count("telegram.recall_deferred")
            continue
        description = result.get("description")
        finished = result.get("ok") is True or description in GONE
        if description in TOO_OLD or response.status_code == 403:
            runtime.count("telegram.recall_too_old")
            with get_db(service.db_path) as db:
                db.execute("UPDATE telegram_deliveries SET attempts=? WHERE fix_id=? AND chat_id=? AND message_id=?",
                           (MAX_ATTEMPTS, fix_id, chat, message))
        elif finished:
            runtime.count("telegram.recall_confirmed")
        else:
            runtime.count("telegram.recall_deferred")
        if finished:
            with get_db(service.db_path) as db:
                db.execute("UPDATE telegram_deliveries SET unavailable=1 WHERE fix_id=? AND chat_id=? AND message_id=?",
                           (fix_id, chat, message))
            done.add(fix_id)
    for fix_id in done:
        if not _left(service.db_path, fix_id):
            deletion_result(service.db_path, fix_id, True)
    return len(jobs)


def expire_deliveries(db_path, *, now=None):
    """Автовидалення копій; не приховує записи архіву й не видаляє навчальні дані."""
    from shared.runtime_settings import _read
    now = time.time() if now is None else now
    with get_db(db_path) as db:
        hours = _read(db)[0]['TELEGRAM_AUTO_DELETE_HOURS']
        if not hours: return 0
        ensure_tables(db)
        return db.execute("""UPDATE telegram_deliveries SET delete_requested=?, next_try=0, attempts=0
            WHERE rowid IN (SELECT rowid FROM telegram_deliveries
                WHERE unavailable=0 AND delete_requested IS NULL AND sent_at IS NOT NULL
                AND sent_at<=? AND sent_at>? ORDER BY sent_at LIMIT 100)""",
            (now, now-hours*3600, now-DELETE_WINDOW_S+SAFETY_S)).rowcount
