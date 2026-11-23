"""Розпізнавач для Pi: той самий інтерфейс, що й WhisperRecognizer, але модель — на Mac."""
import http.client
import time
from urllib.parse import urlsplit

from core.diagnostics import runtime
from core.whisper_result import RecognizerUnavailable, extract_text
from services.asr_protocol import PORT, pack_request, unpack_result


class RemoteModelError(Exception):
    """Помилка конкретної фрази (як збій локального Whisper)."""


class RemoteRecognizer:
    CONNECT_TIMEOUT_S = 3.0
    READ_TIMEOUT_S = 60.0
    COOLDOWN_S = 10.0     # після збою не чекати тайм-ауту на кожній фразі

    def __init__(self, url, language, sample_rate, prompt, log, clock=time.monotonic):
        parts = urlsplit(url if "://" in url else "http://" + url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError("RADIO_ASR_URL: очікується http://адреса:порт")
        self.host, self.port = parts.hostname, parts.port or PORT
        self.language, self.sample_rate, self.prompt, self.log = language, sample_rate, prompt, log
        self.clock = clock
        self._blocked_until = float("-inf")
        self._down = False

    extract_text = staticmethod(extract_text)

    def warmup(self):
        """Pi стартує й без Mac: стан вузла з'ясується на першій фразі."""

    def unload(self):
        """Моделлю керує вузол на Mac."""

    def transcribe(self, audio, prompt=None):
        if self.clock() < self._blocked_until:
            raise RecognizerUnavailable("вузол недавно не відповідав")
        body = pack_request(audio, prompt=self.prompt if prompt is None else prompt,
                            language=self.language, sample_rate=self.sample_rate)
        try:
            result = unpack_result(self._post(body))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self._blocked_until = self.clock() + self.COOLDOWN_S
            runtime.gauge("models.whisper", "unavailable")
            if not self._down:
                self.log.warning("Вузол розшифровки недоступний (%s): фрази відкладаються", type(exc).__name__)
            self._down = True
            raise RecognizerUnavailable(str(exc)) from exc
        if self._down:
            self.log.info("Вузол розшифровки знову доступний")
        self._down = False
        runtime.gauge("models.whisper", "ready")
        return result

    def _post(self, body):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.CONNECT_TIMEOUT_S)
        try:
            conn.connect()
            conn.sock.settimeout(self.READ_TIMEOUT_S)
            conn.request("POST", "/transcribe", body, {"Content-Type": "application/octet-stream"})
            response = conn.getresponse()
            data = response.read()
            if response.status in (400, 413, 500):
                # Вузол живий, але ця фраза не розпізнається: повторювати її вічно не можна.
                raise RemoteModelError(f"вузол відхилив фразу: {response.status}")
            if response.status != 200:
                raise ValueError(f"відповідь {response.status}")
            return data
        finally:
            conn.close()
