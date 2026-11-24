"""Пауза, прослуховування, пристрої й VAD панелі доходять до ефіру без бази; стан вузла — у панель."""
import json
import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import config as real_config
from core.segment_timing import SegmentTiming, TimedSegment
from shared.event_store import init_db
from services.node_bridge import NodeBridge
from services.node_control import NodeControl
from shared.receiver_control import set_listening_hold, set_pause_minutes
from services.receiver_status import init_receiver_db, read_receiver_status
from services.segment_spool import SegmentSpool

LOG = logging.getLogger("test")


@pytest.fixture
def link(tmp_path):
    db = tmp_path / "events.db"
    init_db(db)
    init_receiver_db(db)
    folder = tmp_path / "node"
    ticks = [0.0]
    node = NodeControl(folder, monotonic=lambda: ticks.__setitem__(0, ticks[0] + 1) or ticks[0])
    return SimpleNamespace(db=db, folder=folder, node=node, bridge=NodeBridge(db, folder, real_config, LOG))


def test_pause_and_resume_from_the_panel(link):
    assert link.node.request() is False, "без файла ефір працює"
    set_pause_minutes(link.db, 10)
    link.bridge.step()
    assert link.node.request() == "pause"
    set_pause_minutes(link.db, 0)
    link.bridge.step()
    assert link.node.request() is False


def test_pause_ends_by_itself_when_recognition_service_is_dead(link):
    set_pause_minutes(link.db, 1)
    link.bridge.step()
    link.node.clock = lambda: time.time() + 61
    assert link.node.request() is False


def test_listening_hold_and_its_opt_out(link):
    set_listening_hold(link.db, 30)
    link.bridge.step()
    assert link.node.request() == "listening"
    ticks = [0.0]
    line_input = NodeControl(link.folder, listening_hold=False,
                             monotonic=lambda: ticks.__setitem__(0, ticks[0] + 1) or ticks[0])
    assert line_input.request() is False
    set_pause_minutes(link.db, 5)
    link.bridge.step()
    assert line_input.request() == "pause", "пауза оператора діє завжди"


def test_damaged_or_missing_file_keeps_last_request(link):
    set_pause_minutes(link.db, 10)
    link.bridge.step()
    assert link.node.request() == "pause"
    link.node.path.write_text("{")
    assert link.node.request() == "pause"
    link.node.path.unlink()
    assert link.node.request() == "pause"


@pytest.mark.parametrize("value", ["x", None, True, float("nan"), [1]])
def test_bad_deadlines_do_not_silence_the_air(link, value):
    link.folder.mkdir()
    link.node.path.write_text(json.dumps({"pause_until": value, "hold_until": value}))
    assert link.node.request() is False


def test_far_listening_hold_is_ignored(link):
    link.folder.mkdir()
    link.node.path.write_text(json.dumps({"hold_until": time.time() + 9999}))
    assert link.node.request() is False


def test_panel_settings_reach_the_node_at_start(link):
    from shared.sqlite_connection import get_db
    with get_db(link.db) as db:
        db.execute("CREATE TABLE recognition_settings (id INTEGER PRIMARY KEY, values_json TEXT, revision INTEGER)")
        db.execute("INSERT INTO recognition_settings VALUES (1, ?, 1)", (json.dumps({"VAD_LEVEL": 1, "SILENCE_END_MS": 750}),))
        db.execute("CREATE TABLE audio_device_settings (id INTEGER PRIMARY KEY, values_json TEXT, revision INTEGER)")
        db.execute("INSERT INTO audio_device_settings VALUES (1, ?, 1)", (json.dumps({"RADIO_INPUT_DEVICE": "USB вхід"}),))
    link.bridge.step()
    target = SimpleNamespace(VAD_LEVEL=2, SILENCE_END_MS=600)
    link.node.apply_startup(target)
    assert (target.VAD_LEVEL, target.SILENCE_END_MS, target.RADIO_INPUT_DEVICE) == (1, 750, "USB вхід")


def test_startup_rejects_out_of_range_values(link):
    link.folder.mkdir()
    link.node.path.write_text(json.dumps({"VAD_LEVEL": 9, "SILENCE_END_MS": "600", "MAX_SEGMENT_S": True,
                                          "RADIO_INPUT_DEVICE": "a\nb"}))
    target = SimpleNamespace(VAD_LEVEL=2)
    link.node.apply_startup(target)
    assert vars(target) == {"VAD_LEVEL": 2}


def test_node_state_is_shown_in_the_panel_and_goes_stale(link):
    with link.node.heartbeat(interval=0.05):
        link.node.set("listening")
        time.sleep(0.2)
        link.bridge.step()
        assert read_receiver_status(link.db) == "listening"
        link.node.set("paused")
        time.sleep(0.2)
        link.bridge.step()
        assert read_receiver_status(link.db) == "paused"
    link.bridge.step()
    assert read_receiver_status(link.db) == "offline", "зупинка вузла видна одразу"


def test_old_node_state_is_not_revived(link):
    link.folder.mkdir()
    (link.folder / "state.json").write_text(json.dumps({"state": "listening", "at": time.time() - 60}))
    link.bridge.step()
    assert read_receiver_status(link.db) == "offline"


def test_bridge_survives_database_errors(link, monkeypatch):
    import sqlite3
    calls, stop = [], threading.Event()

    def broken():
        calls.append(1)
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(link.bridge, "step", broken)
    thread = threading.Thread(target=link.bridge.run, args=(stop, 0.01), daemon=True)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(5)
    assert len(calls) > 2


def test_vad_metadata_passes_through_the_queue(tmp_path):
    spool = SegmentSpool(tmp_path / "s")
    pcm = TimedSegment(np.zeros(1600, dtype=np.int16).tobytes(),
                       SegmentTiming(enqueued_at=0.0, enqueued_unix_s=time.time(), audio_end_unix_s=time.time()))
    pcm.vad = {"speech_ms": 900, "end_reason": "silence"}
    spool.put(pcm, 16000)
    assert spool.read(spool.files()[0])[1].vad == pcm.vad
    plain = TimedSegment(bytes(pcm), pcm.timing)
    spool.put(plain, 16000)
    assert spool.read(spool.files()[-1])[1].vad is None or len(spool.files()) == 2


NODE = """
import logging, sys, threading, time
from types import SimpleNamespace
sys.path.insert(0, {root!r})
import shared.audio_devices as devices
class Stream:
    def __enter__(self):
        print("open", flush=True)
        self.stop = threading.Event()
        threading.Thread(target=self.feed, daemon=True).start()
        return self
    def feed(self):
        while not self.stop.wait(0.03):
            self.callback(bytes(960), 480, None, None)
    def __exit__(self, *a):
        self.stop.set()
        print("close", flush=True)
def factory(name, log=None):
    def open_stream(**kw):
        s = Stream(); s.callback = kw["callback"]; return s
    return open_stream
devices.input_stream_factory = factory
import config
from services import radio_node
from services.node_control import NodeControl
stream = SimpleNamespace(live_audio=None, input_muted=None, set_signal_matcher=lambda *a: None, stop=lambda: None)
control = NodeControl({folder!r})
supervisor, _ = radio_node.build_node(config, logging.getLogger("t"), stream_starter=lambda *a, **k: stream, control=control)
supervisor.run()
"""


def test_running_node_closes_and_reopens_input_on_panel_pause(link):
    """Справжній ApplicationSupervisor в окремому процесі; підставний лише звуковий вхід."""
    import os
    import signal
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent)
    process = subprocess.Popen([sys.executable, "-u", "-c", NODE.format(root=root, folder=str(link.folder))],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lines = []
    threading.Thread(target=lambda: lines.extend(iter(process.stdout.readline, "")), daemon=True).start()

    def until(check, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not check():
            link.bridge.step()
            time.sleep(0.1)
        return check()

    events = lambda: [line.strip() for line in lines if line.strip() in ("open", "close")]
    try:
        assert until(lambda: events() == ["open"])
        assert until(lambda: read_receiver_status(link.db) == "listening")
        set_pause_minutes(link.db, 5)
        assert until(lambda: events() == ["open", "close"])
        assert until(lambda: read_receiver_status(link.db) == "paused")
        set_pause_minutes(link.db, 0)
        assert until(lambda: events() == ["open", "close", "open"])
        process.send_signal(signal.SIGINT)
        assert process.wait(20) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
