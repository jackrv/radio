"""Вузол розшифровки на Mac: звук і підказка → результат Whisper. Без бази й Telegram.

Запуск: ``.venv/bin/python -m worker.asr_worker``. Тримає замок приймача, тож не запуститься
поруч із radio.py чи лабораторією (замки — у каталозі цього checkout).
"""
import logging
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

from shared.asr_protocol import MAX_BODY, PORT, pack_result, unpack_request


class Paused(Exception):
    """Модель тимчасово віддано: це «зайнято», а не помилка фрази."""


def handler_for(transcribe, authorized, log):
    busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        def log_message(self, *args):
            pass

        def _send(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not authorized(self.client_address[0]):
                return self._send(403)
            self._send(200 if self.path == "/health" else 404)

        def do_POST(self):
            if not authorized(self.client_address[0]):
                return self._send(403)
            if self.path != "/transcribe":
                return self._send(404)
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send(400)
            if not 0 < size <= MAX_BODY:
                return self._send(413)
            body = self.rfile.read(size)
            try:
                head, audio = unpack_request(body)
            except (ValueError, UnicodeDecodeError):
                return self._send(400)
            # Один запит за раз: модель одна, а зависле попереднє з'єднання не має множити роботу.
            if not busy.acquire(blocking=False):
                return self._send(503)
            try:
                result = pack_result(transcribe(head, audio))
            except Paused:
                return self._send(503)
            except Exception:
                log.exception("Помилка розпізнавання")
                return self._send(500)
            finally:
                busy.release()
            self._send(200, result)

    return Handler


class ModelGate:
    """Модель і її замок: стани ready → paused (віддано лабораторії) → ready.

    Вивантаження чекає кінця поточної фрази; решта запитів тим часом дістає Paused (503).
    """

    def __init__(self, model, pause, acquire, log, close=os.close):
        self.model, self.pause, self.acquire, self.log, self.close = model, pause, acquire, log, close
        # Як у приймачі — один постійний потік моделі. Тексти повторних спроб Whisper (temperature > 0)
        # між прогонами все одно розходяться; причина не встановлена (звіт 2026-11-23-pi-hub).
        self.thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
        self.mutex = threading.Lock()
        self.lock_fd, self.state = None, "waiting"

    def _set(self, state):
        if state != self.state:
            self.log.info("Модель: %s", {"ready": "готова", "paused": "віддано лабораторії",
                                         "waiting": "чекає звільнення"}[state])
        self.state = state
        self.pause.report(state)

    def open(self):
        """Старт: True — модель готова або навмисно на паузі; False — замок тримає хтось інший."""
        if self.pause.requested():
            self._set("paused")
            return True
        self.tick()
        return self.state == "ready"

    def tick(self):
        wanted = not self.pause.requested()
        if self.state == ("ready" if wanted else "paused"):
            return
        with self.mutex:
            if not wanted:
                if self.state == "ready":
                    self.thread.submit(self.model.unload).result()   # збій — лишаємося ready, замок не віддаємо
                if self.lock_fd is not None:
                    self.close(self.lock_fd)
                    self.lock_fd = None
                self._set("paused")
                return
            if self.lock_fd is None:
                self.lock_fd = self.acquire()
            if self.lock_fd is None:
                if self.state != "waiting":
                    self._set("waiting")
                return
            try:
                self.thread.submit(self.model.warmup).result()
            except Exception:
                self.close(self.lock_fd)
                self.lock_fd = None
                raise
            self._set("ready")

    def run(self, stop, interval=1.0):
        while not stop.wait(interval):
            try:
                self.tick()
            except Exception:
                self.log.exception("Не вдалося змінити стан моделі; спробу буде повторено")
                stop.wait(10)

    def transcribe(self, audio, prompt):
        if not self.mutex.acquire(blocking=False):
            raise Paused
        try:
            if self.state != "ready" or self.pause.requested():
                raise Paused
            return self.thread.submit(self.model.transcribe, audio, prompt=prompt).result()
        finally:
            self.mutex.release()

    def finish(self):
        if self.lock_fd is not None:
            self.close(self.lock_fd)
            self.lock_fd = None


def build_transcribe(config, gate):
    def transcribe(head, audio):
        if head.get("sample_rate") != config.SR or head.get("language") != config.LANGUAGE:
            raise ValueError("Частота чи мова не збігаються з вузлом")
        return gate.transcribe(audio, head.get("prompt") or "")

    return transcribe


def main():
    import config
    from shared.instance_lock import acquire_instance_lock
    from shared.recognition import WhisperRecognizer
    from shared.model_exclusion import receiver_model_available
    from shared.model_pause import WORKER_LOCK, ModelPause, model_root
    from shared.radio_stream import TailnetPeers, _QuietServer, client_allowed, tailscale_ipv4

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("radio.asr_worker")
    # Інший checkout — інші замки: для worktree вкажіть каталог основного проєкту.
    root = model_root(config.BASE_DIR)
    host = os.environ.get("RADIO_ASR_HOST", "").strip() or tailscale_ipv4()
    if not host or host in ("0.0.0.0", "::"):
        raise SystemExit("Потрібна адреса Tailscale (або RADIO_ASR_HOST)")
    worker_lock = acquire_instance_lock(root / WORKER_LOCK)
    if worker_lock is None:
        raise SystemExit("Інший вузол розшифровки вже працює")

    def acquire():
        return acquire_instance_lock(root / ".radio345.lock") if receiver_model_available(root) else None

    model = WhisperRecognizer(model_repo=config.MODEL_REPO, language=config.LANGUAGE,
                              sample_rate=config.SR, prompt="", log=log)
    gate = ModelGate(model, ModelPause(root), acquire, log)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: stop.set())
    try:
        if not gate.open():
            raise SystemExit("Модель уже використовує приймач або лабораторія")
        peers = TailnetPeers()
        server = _QuietServer((host, PORT), handler_for(
            build_transcribe(config, gate), lambda ip: client_allowed(ip) and peers.is_owner(ip), log))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="asr-http", daemon=True).start()
        log.info("Вузол розшифровки: %s:%s", host, PORT)
        gate.run(stop)
        server.shutdown()
    finally:
        gate.finish()
        os.close(worker_lock)


if __name__ == "__main__":
    main()
