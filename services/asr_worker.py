"""Вузол розшифровки на Mac: звук і підказка → результат Whisper. Без бази й Telegram.

Запуск: ``.venv/bin/python -m services.asr_worker``. Тримає замок приймача, тож не запуститься
поруч із radio.py чи лабораторією (замки — у каталозі цього checkout).
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from services.asr_protocol import MAX_BODY, PORT, pack_result, unpack_request


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
            except Exception:
                log.exception("Помилка розпізнавання")
                return self._send(500)
            finally:
                busy.release()
            self._send(200, result)

    return Handler


def build_transcribe(config, log):
    from core.recognition import WhisperRecognizer
    model = WhisperRecognizer(model_repo=config.MODEL_REPO, language=config.LANGUAGE,
                              sample_rate=config.SR, prompt="", log=log)
    # Як у приймачі — один постійний потік моделі. Тексти повторних спроб Whisper (temperature > 0)
    # між прогонами все одно розходяться; причина не встановлена (звіт 2026-11-23-pi-hub).
    thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
    thread.submit(model.warmup).result()

    def transcribe(head, audio):
        if head.get("sample_rate") != config.SR or head.get("language") != config.LANGUAGE:
            raise ValueError("Частота чи мова не збігаються з вузлом")
        return thread.submit(model.transcribe, audio, prompt=head.get("prompt") or "").result()

    return transcribe


def main():
    import config
    from core.instance_lock import acquire_instance_lock
    from services.model_exclusion import receiver_model_available
    from services.radio_stream import TailnetPeers, _QuietServer, client_allowed, tailscale_ipv4

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("radio.asr_worker")
    root = Path(config.BASE_DIR)
    if not receiver_model_available(root):
        raise SystemExit("Лабораторія використовує модель")
    lock = acquire_instance_lock(root / ".radio345.lock")
    if lock is None:
        raise SystemExit("Приймач чи інший вузол уже тримає модель")
    try:
        host = os.environ.get("RADIO_ASR_HOST", "").strip() or tailscale_ipv4()
        if not host or host in ("0.0.0.0", "::"):
            raise SystemExit("Потрібна адреса Tailscale (або RADIO_ASR_HOST)")
        peers = TailnetPeers()
        transcribe = build_transcribe(config, log)
        server = _QuietServer((host, PORT), handler_for(
            transcribe, lambda ip: client_allowed(ip) and peers.is_owner(ip), log))
        server.daemon_threads = True
        log.info("Вузол розшифровки: %s:%s", host, PORT)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
