"""Модель віддається лабораторії й повертається: замок, строк, відкладання фраз, очередь тестів."""
import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import radio345
from core.instance_lock import acquire_instance_lock
from core.whisper_result import RecognizerUnavailable
from services import lab_queue
from worker.asr_worker import ModelGate, Paused, build_transcribe, handler_for
from services.model_pause import MAX_PAUSE_S, WORKER_LOCK, ModelPause
from services.remote_recognizer import RemoteRecognizer

LOG = logging.getLogger("test")
AUDIO = np.zeros(1600, dtype=np.float32)


class Model:
    def __init__(self):
        self.events, self.threads, self.fail, self.hold = [], set(), set(), None

    def _note(self, name):
        self.threads.add(threading.get_ident())
        self.events.append(name)
        if name in self.fail:
            raise RuntimeError(name)

    def warmup(self):
        self._note("warmup")

    def unload(self):
        self._note("unload")

    def transcribe(self, audio, prompt):
        self._note("transcribe")
        if self.hold:
            self.hold.wait(5)
        return {"text": "", "segments": []}


@pytest.fixture
def node(tmp_path):
    worker = acquire_instance_lock(tmp_path / WORKER_LOCK)
    pause, model = ModelPause(tmp_path), Model()
    gate = ModelGate(model, pause, lambda: acquire_instance_lock(tmp_path / ".radio345.lock"), LOG)
    yield SimpleNamespace(root=tmp_path, pause=pause, model=model, gate=gate)
    gate.finish()
    os.close(worker)


def model_lock_free(root):
    fd = acquire_instance_lock(root / ".radio345.lock")
    if fd is None:
        return False
    os.close(fd)
    return True


def test_model_and_lock_are_given_away_and_returned(node):
    assert node.gate.open() and not model_lock_free(node.root)
    node.gate.transcribe(AUDIO, "")
    node.pause.start(time.time() + 60)
    with pytest.raises(Paused):
        node.gate.transcribe(AUDIO, "")   # ще до вивантаження нові фрази не беруться
    node.gate.tick()
    assert node.pause.ready() and model_lock_free(node.root)
    node.pause.stop()
    node.gate.tick()
    assert node.pause.restored() and not model_lock_free(node.root)
    assert node.model.events == ["warmup", "transcribe", "unload", "warmup"]
    assert len(node.model.threads) == 1 and threading.get_ident() not in node.model.threads


def test_lab_slot_works_only_while_paused(node):
    from services.model_exclusion import lab_model_slot
    from services.event_store import init_db
    db = node.root / "events.db"
    init_db(db)
    node.gate.open()
    with pytest.raises(RuntimeError):
        with lab_model_slot(db):
            pass
    node.pause.start(time.time() + 60)
    node.gate.tick()
    with lab_model_slot(db):
        node.pause.stop()
        node.gate.tick()
        assert node.gate.state == "waiting" and not node.pause.restored(), "модель ще в лабораторії"
    node.gate.tick()
    assert node.gate.state == "ready"


def test_pause_expires_by_itself(node):
    node.gate.open()
    node.pause.start(time.time() + 0.3)
    node.gate.tick()
    assert node.gate.state == "paused"
    time.sleep(0.4)
    node.gate.tick()
    assert node.gate.state == "ready"


@pytest.mark.parametrize("text", ["", "{", "[]", '{"until": "x"}', '{"until": NaN}', json.dumps({"until": 9e18})])
def test_damaged_request_does_not_take_the_model(node, text):
    node.pause.start(time.time() + 60)
    node.pause.request_path.write_text(text)
    assert node.pause.requested() is False


@pytest.mark.parametrize("delta", [-1, MAX_PAUSE_S + 60])
def test_pause_needs_a_bounded_term(node, delta):
    with pytest.raises(ValueError):
        node.pause.start(time.time() + delta)


def test_unload_failure_keeps_lock_and_model(node):
    node.gate.open()
    node.model.fail.add("unload")
    node.pause.start(time.time() + 60)
    with pytest.raises(RuntimeError):
        node.gate.tick()
    assert node.gate.state == "ready" and not node.pause.ready() and not model_lock_free(node.root)


def test_warmup_failure_releases_lock_and_retries(node):
    node.model.fail.add("warmup")
    with pytest.raises(RuntimeError):
        node.gate.open()
    assert model_lock_free(node.root)
    node.model.fail.clear()
    node.gate.tick()
    assert node.gate.state == "ready"


def test_pause_waits_for_the_current_phrase(node):
    node.gate.open()
    node.model.hold = threading.Event()
    phrase = threading.Thread(target=node.gate.transcribe, args=(AUDIO, ""), daemon=True)
    phrase.start()
    while "transcribe" not in node.model.events:
        time.sleep(0.01)
    node.pause.start(time.time() + 60)
    switch = threading.Thread(target=node.gate.tick, daemon=True)
    switch.start()
    time.sleep(0.2)
    assert node.gate.state == "ready" and "unload" not in node.model.events
    node.model.hold.set()
    switch.join(5)
    assert node.gate.state == "paused"


def test_node_starts_paused_without_loading_the_model(node):
    node.pause.start(time.time() + 60)
    assert node.gate.open() and node.model.events == [] and model_lock_free(node.root)


def test_node_does_not_start_over_a_running_receiver(node):
    receiver = acquire_instance_lock(node.root / ".radio345.lock")
    try:
        assert node.gate.open() is False and node.model.events == []
    finally:
        os.close(receiver)


def test_client_defers_the_phrase_while_model_is_away(node):
    from http.server import ThreadingHTTPServer
    node.gate.open()
    transcribe = build_transcribe(SimpleNamespace(SR=16000, LANGUAGE="uk"), node.gate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(transcribe, lambda ip: True, LOG))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    now = [0.0]
    remote = RemoteRecognizer(f"127.0.0.1:{server.server_address[1]}", "uk", 16000, "", LOG, clock=lambda: now[0])
    try:
        remote.transcribe(AUDIO)
        node.pause.start(time.time() + 60)
        node.gate.tick()
        with pytest.raises(RecognizerUnavailable):
            remote.transcribe(AUDIO)
        node.pause.stop()
        node.gate.tick()
        now[0] = 11.0
        remote.transcribe(AUDIO)
    finally:
        server.shutdown()
        server.server_close()


def background(node):
    stop = threading.Event()
    thread = threading.Thread(target=node.gate.run, args=(stop, 0.05), daemon=True)
    thread.start()
    return stop, thread


def test_cli_pause_and_resume(node, capsys):
    node.gate.open()
    stop, thread = background(node)
    try:
        assert radio345.pause_model(5, node.root, 5) == 0 and model_lock_free(node.root)
        assert radio345.resume_model(node.root, 5) == 0 and not model_lock_free(node.root)
        assert radio345.pause_model(0, node.root, 1) == 2
    finally:
        stop.set()
        thread.join(5)
    assert radio345.pause_model(5, node.root, 0.5) == 1 and not node.pause.requested(), "без відповіді запит знято"


def test_cli_without_worker(tmp_path):
    assert radio345.pause_model(5, tmp_path, 1) == 3 and radio345.resume_model(tmp_path, 1) == 3
    assert not (tmp_path / ".radio345-launcher/model-pause.json").exists()


def test_lab_queue_uses_the_worker_when_it_is_running(node, monkeypatch):
    import config
    monkeypatch.setattr(config, "BASE_DIR", node.root)
    node.gate.open()
    stop, thread = background(node)
    seen = []

    class Process:
        pid, returncode = 1, 0

        def poll(self):
            seen.append((node.gate.state, model_lock_free(node.root)))
            return 0

    queue = node.root / "experiments"
    queue.mkdir()
    preset = next(iter(lab_queue.PRESETS))
    lab_queue.enqueue(queue, preset) if hasattr(lab_queue, "enqueue") else lab_queue.add(queue, preset)
    try:
        lab_queue.run(queue, launcher=SimpleNamespace(receiver_stopped=lambda: False), spawn=lambda a, l: Process(),
                      sleep=lambda s: time.sleep(min(s, 0.05)), estimate=lambda p: 60, cooldown=0)
        assert seen and seen[0] == ("paused", True)
        deadline = time.monotonic() + 5
        while node.gate.state != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert node.pause.restored()
    finally:
        stop.set()
        thread.join(5)


def test_r345_removes_stale_old_launcher_state(tmp_path):
    from services.launcher_lifecycle import managed_status
    control = radio345.Control(tmp_path / ".radio345-launcher")
    control.directory.mkdir()
    (control.directory / "state.json").write_text('{"stopped_services": ["radio"]}')
    control.acquire()
    control.save("t", [])
    try:
        status = managed_status(tmp_path)
        assert status["state"] == "r345" and status["stopped_services"] == [] and not status["launcher_reachable"]
    finally:
        control.release()
    assert managed_status(tmp_path)["managed"] is False


def test_pause_is_confirmed_even_if_the_node_was_waiting(node):
    other = acquire_instance_lock(node.root / ".radio345.lock")
    node.gate.tick()
    assert node.gate.state == "waiting"
    node.pause.start(time.time() + 60)
    node.gate.tick()
    assert node.pause.ready()
    os.close(other)


def test_pause_follows_the_lock_root_of_the_node(tmp_path, monkeypatch):
    from services.model_pause import model_root
    monkeypatch.setenv("RADIO_ASR_LOCK_ROOT", str(tmp_path))
    assert radio345.lock_root() == tmp_path == model_root("/інше")
    assert model_root("/інше", {}) == radio345.Path("/інше")
