"""Состояние приёмника между процессами Radio 345 и API."""

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from services.sqlite_connection import get_db


def init_receiver_db(db_path):
    with get_db(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS receiver_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                instance_id TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)


def record_heartbeat(db_path, instance_id, *, now=None):
    timestamp = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        db.execute("""
            INSERT INTO receiver_runtime
                (id, instance_id, state, updated_at)
            VALUES (1, ?, 'listening', ?)
            ON CONFLICT(id) DO UPDATE SET
                instance_id = excluded.instance_id,
                state = excluded.state,
                updated_at = excluded.updated_at
        """, (instance_id, timestamp))


def mark_stopped(db_path, instance_id):
    with get_db(db_path) as db:
        db.execute("""
            UPDATE receiver_runtime
            SET state = 'stopped', updated_at = ?
            WHERE id = 1 AND instance_id = ?
        """, (time.time(), instance_id))


def read_receiver_status(db_path, *, now=None, timeout=10):
    timestamp = time.time() if now is None else float(now)

    with get_db(db_path) as db:
        exists = db.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'receiver_runtime'
        """).fetchone()

        if not exists:
            return "offline"

        row = db.execute("""
            SELECT state, updated_at
            FROM receiver_runtime WHERE id = 1
        """).fetchone()

    if row is None:
        return "offline"

    state, updated_at = row

    if (
        state == "listening"
        and -5 <= timestamp - updated_at <= timeout
    ):
        return "listening"

    return "offline"


@contextmanager
def receiver_heartbeat(db_path, log=None, interval=2.0):
    """Публикует состояние, пока открыт аудиопоток."""

    log = log or logging.getLogger("radio")
    instance_id = uuid.uuid4().hex
    stop_event = threading.Event()

    try:
        init_receiver_db(db_path)
        record_heartbeat(db_path, instance_id)
    except sqlite3.Error:
        log.exception("Не удалось запустить монитор состояния")
        yield
        return

    def update():
        while not stop_event.wait(interval):
            try:
                record_heartbeat(db_path, instance_id)
            except sqlite3.Error:
                log.exception("Ошибка обновления состояния приёмника")

    thread = threading.Thread(
        target=update,
        name="radio-heartbeat",
        daemon=True,
    )
    thread.start()

    try:
        yield
    finally:
        stop_event.set()
        thread.join()

        try:
            mark_stopped(db_path, instance_id)
        except sqlite3.Error:
            log.exception("Ошибка сохранения остановки приёмника")
