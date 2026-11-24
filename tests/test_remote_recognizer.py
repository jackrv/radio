"""Мережевий розпізнавач: Mac пропав, повернувся, зайнятий, завис; фрази не губляться."""
import logging
import socket
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from core.transcriber import Transcriber
from core.whisper_result import RecognizerUnavailable
from services.asr_protocol import pack_request, unpack_request
from worker.asr_worker import handler_for
from services.recordings import RecordingStore
from services.remote_recognizer import RemoteModelError, RemoteRecognizer

LOG = logging.getLogger("test")
AUDIO = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
RESULT = {"text": " клен", "segments": [{"start": 0.0, "end": 1.0, "text": " клен", "avg_logprob": np.float64(-0.2),
                                        "no_speech_prob": 0.1, "compression_ratio": 1.0, "tokens": [1]}]}


@pytest.fixture
def serve():
    servers = []

    def start(transcribe, authorized=lambda ip: True, port=0):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(transcribe, authorized, LOG))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def client(port, clock=None):
    now = clock or (lambda: 0.0)
    return RemoteRecognizer(f"127.0.0.1:{port}", "uk", 16000, "дуб", LOG, clock=now)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_audio_and_prompt_arrive_exactly(serve):
    seen = {}

    def transcribe(head, audio):
        seen.update(head=head, audio=audio)
        return RESULT

    remote = client(serve(transcribe))
    result = remote.transcribe(AUDIO)
    assert np.array_equal(seen["audio"], AUDIO) and seen["head"]["prompt"] == "дуб"
    assert remote.extract_text(result) == "клен"
    remote.transcribe(AUDIO, prompt="")
    assert seen["head"]["prompt"] == ""


def test_mac_disappears_and_returns(serve):
    now = [0.0]
    port = free_port()
    remote = client(port, lambda: now[0])
    with pytest.raises(RecognizerUnavailable):
        remote.transcribe(AUDIO)
    serve(lambda head, audio: RESULT, port=port)
    with pytest.raises(RecognizerUnavailable):
        remote.transcribe(AUDIO)   # пауза після збою
    now[0] = 11.0
    assert remote.extract_text(remote.transcribe(AUDIO)) == "клен"


def test_hung_mac_is_unavailable(serve):
    release = threading.Event()
    remote = client(serve(lambda head, audio: release.wait(10) and RESULT))
    remote.READ_TIMEOUT_S = 0.2
    with pytest.raises(RecognizerUnavailable):
        remote.transcribe(AUDIO)
    release.set()


def test_busy_and_forbidden_are_unavailable(serve):
    started, release = threading.Event(), threading.Event()

    def slow(head, audio):
        started.set()
        release.wait(10)
        return RESULT

    port = serve(slow)
    first = threading.Thread(target=client(port).transcribe, args=(AUDIO,), daemon=True)
    first.start()
    assert started.wait(5)
    with pytest.raises(RecognizerUnavailable):
        client(port).transcribe(AUDIO)
    release.set()
    first.join(5)
    with pytest.raises(RecognizerUnavailable):
        client(serve(lambda head, audio: RESULT, authorized=lambda ip: False)).transcribe(AUDIO)


def test_model_error_is_not_retried_forever(serve):
    def broken(head, audio):
        raise RuntimeError("модель")

    with pytest.raises(RemoteModelError):
        client(serve(broken)).transcribe(AUDIO)


@pytest.mark.parametrize("body", [b"", b"\x01", b"\xff\xff\xff\xff{}", pack_request(AUDIO, prompt="", language="uk",
                                                                                  sample_rate=16000)[:-1]])
def test_damaged_request_is_rejected(body):
    with pytest.raises(ValueError):
        unpack_request(body)


class Remote:
    prompt = ""

    def __init__(self, up):
        self.up = up

    def transcribe(self, audio, prompt=None):
        if not self.up:
            raise RecognizerUnavailable("немає")
        return {"segments": []}

    extract_text = staticmethod(lambda result: "")

    def warmup(self):
        pass

    def unload(self):
        pass


def test_phrase_is_deferred_while_mac_is_down_then_recognised_once(tmp_path):
    store = RecordingStore(tmp_path / "rec")
    submitted = []
    recorder = SimpleNamespace(submit=lambda pcm, sr, data: submitted.append((pcm, sr, data)), store=store)
    remote = Remote(up=False)
    worker = Transcriber(segments=None, recognizer=remote, process_text=lambda t: t, event_processor=None,
                         sample_rate=16000, min_rms=0, normalize_audio=False, debug_rms=False, blacklist=(),
                         log=LOG, recorder=recorder)
    worker.deferred = store
    pcm = np.full(16000, 1500, dtype=np.int16).tobytes()
    worker.process_segment(pcm)
    assert [d["status"] for _, _, d in submitted] == ["deferred"]

    data, sr = dict(submitted[0][2]), submitted[0][1]
    store.save(pcm, sr, data)
    assert worker._process_one_deferred() and store.next_deferred() is not None, "Mac ще спить — запис лишається"
    remote.up = True
    worker._deferred_idle_until = 0
    assert worker._process_one_deferred()
    assert store.next_deferred() is None and len(submitted) == 1


def test_pi_code_does_not_import_mlx():
    probe = ("import sys, services.transcriber_factory, services.remote_recognizer, core.transcriber;"
             "sys.exit(any(m.split('.')[0].startswith('mlx') for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).resolve().parent.parent).returncode == 0
