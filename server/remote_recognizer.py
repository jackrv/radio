"""Розпізнавач для Pi: той самий інтерфейс, що й WhisperRecognizer, але модель — на Mac."""
import http.client
import time
from urllib.parse import urlsplit

from shared.diagnostics import runtime
from shared.whisper_result import RecognizerUnavailable, extract_text
from shared.asr_protocol import PORT, pack_request, unpack_result


class RemoteModelError(Exception):
    """Помилка конкретної фрази (як збій локального Whisper)."""


class _Refused(ValueError):
    """Вузол відповів, але фразу не взяв (503: зайнятий або модель віддано)."""


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
        self.down_since = None   # monotonic; None — вузол відповідав останнього разу
        self.on_state = None     # (sending | ready | refused | unavailable) — для схеми панелі

    extract_text = staticmethod(extract_text)

    def warmup(self):
        """Pi стартує й без Mac: стан вузла з'ясується на першій фразі."""

    def unload(self):
        """Моделлю керує вузол на Mac."""

    def _report(self, state):
        if self.on_state is None:
            return
        try:
            self.on_state(state)
        except Exception:
            runtime.count("whisper_state.error")   # показ стану не заважає розпізнаванню

    def transcribe(self, audio, prompt=None):
        if self.clock() < self._blocked_until:
            raise RecognizerUnavailable("вузол недавно не відповідав")
        body = pack_request(audio, prompt=self.prompt if prompt is None else prompt,
                            language=self.language, sample_rate=self.sample_rate)
        self._report("sending")
        try:
            result = unpack_result(self._post(body))
        except RemoteModelError:
            self._report("ready")
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self._blocked_until = self.clock() + self.COOLDOWN_S
            runtime.gauge("models.whisper", "unavailable")
            self._report("refused" if isinstance(exc, _Refused) else "unavailable")
            if not self._down:
                self.down_since = time.monotonic()
                self.log.warning("Вузол розшифровки недоступний (%s): фрази відкладаються", type(exc).__name__)
            self._down = True
            raise RecognizerUnavailable(str(exc)) from exc
        if self._down:
            self.log.info("Вузол розшифровки знову доступний")
        self._down = False
        self.down_since = None
        runtime.gauge("models.whisper", "ready")
        self._report("ready")
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
                raise _Refused(f"відповідь {response.status}")
            return data
        finally:
            conn.close()
