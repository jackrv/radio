"""Состояние приймача между процессами Radio 345 и API."""

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from services.sqlite_connection import get_db
from core.diagnostics import runtime



class ReceiverRuntimeState:
    """Фактическое состояние аудиопотока."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "stopped"

    def set(self, state):
        if state not in ("listening", "paused", "stopped"):
            raise ValueError("Неприпустимий стан приймача")
        with self._lock:
            self._state = state

    def get(self):
        with self._lock:
            return self._state


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


def record_heartbeat(db_path, instance_id, *, now=None, state="listening"):
    timestamp = time.time() if now is None else float(now)

    if state not in ("listening", "paused", "stopped"):
        raise ValueError("Неприпустимий стан heartbeat")

    with get_db(db_path) as db:
        db.execute("""
            INSERT INTO receiver_runtime
                (id, instance_id, state, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                instance_id = excluded.instance_id,
                state = excluded.state,
                updated_at = excluded.updated_at
        """, (instance_id, state, timestamp))


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
        state in ("listening", "paused")
        and -5 <= timestamp - updated_at <= timeout
    ):
        return state

    return "offline"


@contextmanager
def receiver_heartbeat(db_path, log=None, interval=2.0, state_provider=None):
    """Публикует состояние, пока открыт аудиопоток."""

    log = log or logging.getLogger("radio")
    state_provider = state_provider or (lambda: "listening")
    instance_id = uuid.uuid4().hex
    stop_event = threading.Event()

    try:
        init_receiver_db(db_path)
        record_heartbeat(db_path, instance_id, state=state_provider())
    except sqlite3.Error:
        log.exception("Не вдалося запустити монітор стану")
        yield
        return

    def update():
        while not stop_event.wait(interval):
            try:
                record_heartbeat(db_path, instance_id, state=state_provider())
                runtime.flush()
            except sqlite3.Error:
                log.exception("Помилка оновлення стану приймача")

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
            log.exception("Помилка збереження зупинки приймача")


def another_receiver_running(db_path, *, now=None):
    """Чи працює інший приймач (свіжий heartbeat «listening»/«paused»).

    Після штатної зупинки стан «stopped», тож перезапуск не блокується.
    Після аварії heartbeat застаріває за 10 с.
    """
    try:
        return read_receiver_status(db_path, now=now) in ("listening", "paused")
    except sqlite3.Error:
        return False
