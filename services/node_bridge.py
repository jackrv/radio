"""Бік бази: панельні пауза, прослуховування й налаштування → control.json; стан вузла → монітор приймача."""
import logging
import sqlite3
import threading
import time
import uuid

from services.node_control import DEVICE_KEYS, STATES, VAD_KEYS, read_json, write_json
from shared.receiver_control import ensure_hold_table, ensure_table
from services.receiver_status import init_receiver_db, record_heartbeat
from shared.sqlite_connection import get_db

FRESH_S = 10.0


class NodeBridge:
    def __init__(self, db_path, folder, config, log=None, clock=time.time):
        self.db_path, self.folder, self.config, self.clock = db_path, folder, config, clock
        self.log = log or logging.getLogger("radio")
        self.instance, self._last, self._settings_at, self._settings = uuid.uuid4().hex, None, float("-inf"), {}
        self._heartbeat_at = 0.0

    def _read(self):
        with get_db(self.db_path) as db:
            ensure_table(db)
            ensure_hold_table(db)
            pause = db.execute("SELECT pause_until FROM receiver_control WHERE id=1").fetchone()
            hold = db.execute("SELECT hold_until FROM receiver_listening_hold WHERE id=1").fetchone()
        return {"pause_until": float(pause[0]) if pause else 0.0, "hold_until": float(hold[0]) if hold else 0.0}

    def _startup(self):
        from services.management.audio_settings import read_audio_settings
        from services.management.settings import read_settings
        vad = read_settings(self.db_path)["values"]
        devices = read_audio_settings(self.db_path)["values"]
        return {**{k: vad[k] for k in VAD_KEYS}, **{k: devices[k] for k in DEVICE_KEYS}}

    def step(self):
        now = time.monotonic()
        if now - self._settings_at >= 5:
            self._settings, self._settings_at = self._startup(), now
        data = {**self._read(), **self._settings}
        if data != self._last:
            write_json(self.folder / "control.json", data)
            self._last = data
        state = read_json(self.folder / "state.json") or {}
        at = state.get("at")
        if (state.get("state") in STATES and type(at) in (int, float) and at != self._heartbeat_at
                and -5 <= self.clock() - at <= FRESH_S):
            record_heartbeat(self.db_path, self.instance, now=at, state=state["state"])
            self._heartbeat_at = at

    def run(self, stop, interval=0.5):
        failing = False
        while not stop.wait(interval):
            try:
                self.step()
                failing = False
            except (sqlite3.Error, OSError, ValueError, KeyError):
                if not failing:
                    self.log.exception("Керування вузлом тимчасово недоступне; повторюю")
                failing = True

    def start(self, stop):
        init_receiver_db(self.db_path)
        thread = threading.Thread(target=self.run, args=(stop,), name="node-bridge", daemon=True)
        thread.start()
        return thread
