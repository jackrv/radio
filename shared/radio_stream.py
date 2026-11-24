"""Ефір для застосунку на iPhone через Tailscale: потоки, історія фраз, PTT.

Безперервні MP3-потоки: тиша, а коли є мовлення (чи фраза пройшла той самий
відбір, що й голосові Telegram) — його аудіо; безперервність тримає застосунок у фоні.

Слухає лише конкретну адресу (типово Tailscale IPv4), не 0.0.0.0. Доступ без
токена: лише пристрої того самого облікового запису Tailscale, що й цей вузол
(`tailscale status --json`; пристрої, якими з вами поділилися, — ні). Див. TailnetPeers.
"""

import base64
import collections
import hashlib
import ipaddress
import itertools
import json
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CHUNK_S = 0.1
MAX_PENDING_S = 120.0       # непрогране понад це відкидається з найстаріших
LIVE_MAX_PENDING_S = 1.5    # наживо: відставання понад це скидаємо, затримка не росте
# Запас при підключенні (як burst-on-connect в інтернет-радіо): без нього плеєр iPhone
# отримує рівно реальний час і зупиняється на «буферизацію» від кожного ривка мережі.
BURST_S = 3.0
LIVE_BURST_S = 1.5
BYTES_PER_S = 4000          # 32 кбіт/с
# «Наживо без сигналів»: скільки притримувати початок сегмента (див. SignalGate).
HOLD_PREROLL_S = 0.5        # PREROLL_MS приймача
HOLD_MARGIN_S = 0.2
MAX_HOLD_S = 4.0
# PTT з iPhone: PCM16 моно 16 кГц у бінарних повідомленнях WebSocket.
TX_MAX_S = 120.0            # довша передача обривається (захист від завислої кнопки)
TX_IDLE_S = 3.0             # без даних стільки — з'єднання вважається обірваним
TX_TAIL_S = 0.8             # після передачі приймач ще не слухає (луна, хвіст динаміків)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HISTORY = 30                # останні фрази для списку й повтору
CLIENT_QUEUE = 1500         # пакети ffmpeg ~144 байти: ≈ 50 с запасу на повільного клієнта
BITRATE = "32k"
TAILSCALE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TAILNET = ipaddress.ip_network("100.64.0.0/10")
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def tailscale_ipv4():
    """IPv4 цього Mac у tailnet або None (Tailscale вимкнено чи не встановлено)."""
    cli = shutil.which("tailscale") or (TAILSCALE_CLI if os.path.exists(TAILSCALE_CLI) else None)
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.split():
        try:
            if ipaddress.ip_address(line) in TAILNET:
                return line
        except ValueError:
            continue
    return None


def _ip(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def client_allowed(address):
    ip = _ip(address)
    return bool(ip) and (ip.is_loopback or ip in TAILNET or ip in TAILNET6)


# Контракт застосунків (docs/app-api.md). Нові необов'язкові поля версію не змінюють;
# несумісна зміна — API_VERSION + 1, попередня версія працює, доки є в API_SUPPORTED.
API_VERSION = 1
API_SUPPORTED = (1,)


class ClipMixer:
    """Черга фраз → безперервний PCM у реальному часі (тиша між фразами)."""

    def __init__(self, sample_rate, max_pending_s=None):
        self.sample_rate = sample_rate
        self.chunk_bytes = int(sample_rate * CHUNK_S) * 2
        self.max_pending = int(sample_rate * (MAX_PENDING_S if max_pending_s is None else max_pending_s)) * 2
        self._pending = collections.deque()
        self._pending_bytes = 0
        self._lock = threading.Lock()
        self.dropped = 0

    def add(self, pcm):
        pcm = bytes(pcm)
        if len(pcm) % 2:
            pcm = pcm[:-1]
        with self._lock:
            self._pending.append(memoryview(pcm))
            self._pending_bytes += len(pcm)
            while self._pending_bytes > self.max_pending and len(self._pending) > 1:
                self._pending_bytes -= len(self._pending.popleft())
                self.dropped += 1

    def next_chunk(self):
        out = bytearray()
        with self._lock:
            while len(out) < self.chunk_bytes and self._pending:
                head = self._pending[0]
                take = min(len(head), self.chunk_bytes - len(out))
                out += head[:take]
                self._pending_bytes -= take
                if take == len(head):
                    self._pending.popleft()
                else:
                    self._pending[0] = head[take:]
        out += bytes(self.chunk_bytes - len(out))
        return bytes(out)

    @property
    def pending_s(self):
        with self._lock:
            return self._pending_bytes / 2 / self.sample_rate


def ws_read_frame(rfile):
    """Один кадр WebSocket від клієнта: (opcode, payload) або (None, b"") при закритті."""
    head = rfile.read(2)
    if len(head) < 2:
        return None, b""
    b1, b2 = head
    if not b1 & 0x80:
        raise ValueError("фрагментовані повідомлення не підтримуються")
    opcode, length = b1 & 0x0F, b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    if length > 1 << 20:
        raise ValueError("завеликий кадр")
    mask = rfile.read(4) if b2 & 0x80 else b""
    payload = rfile.read(length)
    if len(payload) < length:
        return None, b""
    if mask:
        key = int.from_bytes((mask * (length // 4 + 1))[:length], "big")
        payload = (int.from_bytes(payload, "big") ^ key).to_bytes(length, "big")
    return opcode, payload


def ws_frame(opcode, payload=b""):
    if len(payload) > 125:
        raise ValueError("керуючий кадр задовгий")
    return bytes([0x80 | opcode, len(payload)]) + payload


class NullOutput:
    """Вихід «у порожнечу» (RADIO_OUTPUT_DEVICE=null): передача йде як справжня, звук нікуди не потрапляє."""

    def __call__(self, sample_rate):
        from contextlib import nullcontext
        return nullcontext(lambda pcm: None)


class SharedSpeaker:
    """Типовий вихід Mac (динаміки), відкритий між передачами ще ``idle_s``:
    відкриття аудіопристрою на кожне натискання PTT забирало початок фрази."""

    def __init__(self, idle_s=60.0, device=""):
        self.idle_s = idle_s
        self.device = device  # частина назви (RADIO_OUTPUT_DEVICE); порожньо — системний вихід
        self.out = None
        self.lock = threading.Lock()
        self.timer = None

    def __call__(self, sample_rate):
        speaker = self

        class Session:
            def __enter__(self):
                return speaker.acquire(sample_rate)

            def __exit__(self, *exc):
                speaker.release()
                return False

        return Session()

    def acquire(self, sample_rate):
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            if self.out is None:
                import sounddevice as sd
                from shared.audio_devices import find_device
                index = find_device(self.device, "output")  # DeviceNotFound — передача відмовляє
                self.out = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16",
                                              latency="low", device=index)
                self.out.start()
            return self.out.write

    def release(self):
        with self.lock:
            self.timer = threading.Timer(self.idle_s, self.close)
            self.timer.daemon = True
            self.timer.start()

    def close(self):
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            out, self.out = self.out, None
        if out is not None:
            try:
                out.stop()
            finally:
                out.close()


def runtime_count(name):
    try:
        from shared.diagnostics import runtime
        runtime.count(name)
    except Exception:
        pass


class SignalGate:
    """Канал «наживо без сигналів»: початок сегмента притримується, доки не ясно,
    чи це звук сповіщення.

    Сегмент, що закінчився за ``hold_s``, оцінюється тим самим спектральним фільтром,
    що й у приймачі; сигнал не грається. Довший сегмент сигналом бути не може
    (зразок + звук поза збігом + preroll/тиша) — його відпускаємо, далі він іде наживо.
    Ціна — затримка початку кожної передачі на ``hold_s``.
    """

    def __init__(self, sample_rate, emit, matcher=None, mode_getter=None):
        self.sample_rate = sample_rate
        self.emit = emit
        self.matcher = matcher
        self.mode_getter = mode_getter
        self.held = None
        self.held_bytes = 0
        self.dropped = 0

    @property
    def active(self):
        m = self.matcher
        mode = self.mode_getter() if self.mode_getter else "active"
        return m is not None and bool(m.templates) and mode != "off"

    @property
    def hold_s(self):
        from shared.notification_spectral import HOP
        longest = max((t.frames for t in self.matcher.templates), default=0) * HOP / self.sample_rate
        return min(HOLD_PREROLL_S + longest + self.matcher.max_outside_s + HOLD_MARGIN_S, MAX_HOLD_S)

    def feed(self, pcm, phase):
        if phase == "start":
            if self.held is not None:  # попередній сегмент без «end»: не губимо
                self.emit(b"".join(self.held))
            if not self.active:
                self.held = None
                self.emit(pcm)
                return
            self.held, self.held_bytes = [pcm], len(pcm)
            return
        if self.held is None:
            self.emit(pcm)
            return
        self.held.append(pcm)
        self.held_bytes += len(pcm)
        if phase == "end":
            segment = b"".join(self.held)
            self.held = None
            if self._is_signal(segment):
                self.dropped += 1
                runtime_count("radio_stream.signal_dropped")
            else:
                self.emit(segment)
        elif self.held_bytes / 2 / self.sample_rate > self.hold_s:
            self.emit(b"".join(self.held))
            self.held = None

    def _is_signal(self, segment):
        if not self.active:
            return False
        result = self.matcher.score_pcm(segment)
        if result is None:
            return False
        if isinstance(result, dict):
            score, outside, extra = result.get("score"), result.get("outside_s"), result.get("extra_ratio")
        else:
            score, outside, extra = (getattr(result, k, None) for k in ("score", "outside_s", "extra_ratio"))
        if score is None:
            return False
        return bool(self.matcher.is_candidate(score, outside, extra))


class TailnetPeers:
    """Знімок ``tailscale status --json`` (кеш 15 с):

    * ``is_owner(ip)`` — пристрій того самого облікового запису Tailscale, що й цей
      вузол (замість токена). Loopback — свій. Невідома адреса оновлює знімок
      позачергово, але не частіше ніж раз на 2 с: щойно під'єднаний iPhone не чекає.
    * ``is_home(ip)`` — iPhone у тій самій домашній мережі: Tailscale з'єднаний
      напряму через приватну адресу (не через ретранслятор DERP).

    Tailscale недоступний — нікого, крім loopback, не пускаємо."""

    TTL_S = 15.0
    MISS_REFRESH_S = 2.0

    def __init__(self, status_fn=None):
        self.status_fn = status_fn or _tailscale_status
        self._at = float("-inf")
        self._peers = {}
        self._lock = threading.Lock()

    def _get(self, ip, refresh_missing=False):
        with self._lock:
            age = time.monotonic() - self._at
            if age > self.TTL_S or (refresh_missing and ip not in self._peers and age > self.MISS_REFRESH_S):
                try:
                    self._peers = _peer_table(self.status_fn())
                except Exception:
                    self._peers = {}
                self._at = time.monotonic()
            return self._peers.get(ip)

    def is_owner(self, address):
        ip = _ip(address)
        if ip is None:
            return False
        if ip.is_loopback:
            return True
        peer = self._get(str(ip), refresh_missing=True)
        return bool(peer and peer["owner"])

    def is_home(self, address):
        ip = _ip(address)
        peer = self._get(str(ip)) if ip is not None else None
        return bool(peer and peer["local"])


def _tailscale_status():
    cli = shutil.which("tailscale") or (TAILSCALE_CLI if os.path.exists(TAILSCALE_CLI) else None)
    if not cli:
        return {}
    out = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=5).stdout
    return json.loads(out or "{}")


def _peer_table(status):
    """{tailscale_ip: {"owner": той самий обліковий запис, "local": пряме з'єднання
    через приватну адресу}}. Власні адреси вузла теж «свої»."""
    me = (status.get("Self") or {}).get("UserID")
    result = {}
    for tip in (status.get("Self") or {}).get("TailscaleIPs") or ():
        result[tip] = {"owner": me is not None, "local": False}
    for peer in (status.get("Peer") or {}).values():
        addr = (peer.get("CurAddr") or "").rsplit(":", 1)[0].strip("[]")
        ip = _ip(addr) if addr else None
        local = bool(ip and ip.is_private and ip not in TAILNET and not ip.is_loopback)
        owner = me is not None and peer.get("UserID") == me
        for tip in peer.get("TailscaleIPs") or ():
            result[tip] = {"owner": owner, "local": local}
    return result


class _QuietServer(ThreadingHTTPServer):
    """iPhone рве з'єднання при блокуванні/зміні мережі — це не помилка сервера."""

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Channel:
    """Один безперервний MP3-потік: мікшер → ffmpeg → черги слухачів."""

    def __init__(self, name, sample_rate, max_pending_s, log, burst_s=0.0):
        self.name = name
        self.burst_bytes = int(burst_s * BYTES_PER_S)
        self.recent = collections.deque()
        self.recent_bytes = 0
        self.mixer = ClipMixer(sample_rate, max_pending_s)
        self.sample_rate = sample_rate
        self.log = log
        self.clients = set()
        self.lock = threading.Lock()
        self.ffmpeg = None

    def start(self, stop, threads):
        self.ffmpeg = subprocess.Popen(
            encoder_cmd(self.sample_rate), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        for target, suffix in ((lambda: self._feed_loop(stop), "feed"), (self._fanout_loop, "fanout")):
            t = threading.Thread(target=target, name=f"radio-stream-{self.name}-{suffix}", daemon=True)
            t.start()
            threads.append(t)

    def _feed_loop(self, stop):
        next_at = time.monotonic()
        while not stop.is_set():
            try:
                self.ffmpeg.stdin.write(self.mixer.next_chunk())
                self.ffmpeg.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                if not stop.is_set() and self.log:
                    self.log.error("Потік для iPhone (%s): кодувальник ffmpeg зупинився", self.name)
                return
            next_at += CHUNK_S
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            elif delay < -1.0:
                next_at = time.monotonic()  # після сну Mac не наздоганяємо

    def _fanout_loop(self):
        while True:
            data = self.ffmpeg.stdout.read(4096)
            if not data:
                return
            with self.lock:
                clients = list(self.clients)
                if self.burst_bytes:
                    self.recent.append(data)
                    self.recent_bytes += len(data)
                    while self.recent and self.recent_bytes - len(self.recent[0]) >= self.burst_bytes:
                        self.recent_bytes -= len(self.recent.popleft())
            for q in clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    self.drop_client(q)

    def add_client(self):
        q = queue.Queue(CLIENT_QUEUE)
        with self.lock:
            for data in self.recent:
                q.put_nowait(data)
            self.clients.add(q)
        return q

    def drop_client(self, q):
        with self.lock:
            self.clients.discard(q)
        # Повна черга не має завадити сигналу «кінець»: інакше обробник висів би на get().
        while True:
            try:
                q.put_nowait(None)
                return
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass

    def has_client(self, q):
        with self.lock:
            return q in self.clients

    @property
    def listeners(self):
        with self.lock:
            return len(self.clients)

    def stop(self):
        if self.ffmpeg is not None:
            try:
                self.ffmpeg.stdin.close()
            except OSError:
                pass
            try:
                self.ffmpeg.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.ffmpeg.kill()
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            self.drop_client(q)


def encoder_cmd(sample_rate, live=True):
    return [
        "ffmpeg", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", BITRATE, "-f", "mp3",
        *(("-flush_packets", "1") if live else ()), "pipe:1",
    ]


class RadioStream:
    """Канали: ``selected`` — фрази після розпізнавання й відбору;
    ``live`` — усе мовлення за VAD одразу, без текстового відбору."""

    def __init__(self, *, sample_rate, host, port, mode="watch", log=None, transmit=False,
                 player_factory=None, ptt_key=None, ptt_lead_s=0.0, ptt_tail_s=0.0, peers=None):
        if mode not in ("all", "watch", "commands"):
            raise ValueError(f"Невідомий режим потоку: {mode}")
        self.sample_rate = sample_rate
        self.host, self.port, self.mode = host, port, mode
        self.log = log
        self.channels = {
            "selected": _Channel("selected", sample_rate, MAX_PENDING_S, log, BURST_S),
            "live": _Channel("live", sample_rate, LIVE_MAX_PENDING_S, log, LIVE_BURST_S),
            "live_clean": _Channel("live_clean", sample_rate, LIVE_MAX_PENDING_S + MAX_HOLD_S, log, LIVE_BURST_S),
        }
        self.gate = SignalGate(sample_rate, self.channels["live_clean"].mixer.add)
        self._gate_queue = queue.Queue(2000)
        self.peers = peers or TailnetPeers()
        # Передача: поки що звук грає на динаміках Mac; з передавачем — на його вхід + PTT.
        self.transmit_enabled = bool(transmit)
        self.player_factory = player_factory or SharedSpeaker()
        # Ключ PTT рації (shared/ptt_key.py); None — без ключа (VOX або динаміки Mac).
        self.ptt_key = ptt_key
        self.ptt_lead_s = ptt_lead_s   # рації потрібен час перейти на передачу до звуку
        self.ptt_tail_s = ptt_tail_s   # ключ ще тримається, поки звук виходить із буфера
        self._tx_lock = threading.Lock()
        self._tx_active = False
        self._tx_until = 0.0
        self.level = None              # вимірювач рівня входу (server/level_meter.py)
        self._air_at = float("-inf")   # останній кадр мовлення з ефіру
        self.mixer = self.channels["selected"].mixer
        self._history = collections.deque(maxlen=HISTORY)
        self._history_lock = threading.Lock()
        self._ids = itertools.count(1)
        self.session = os.urandom(6).hex()   # нумерація фраз починається знову з кожним запуском
        self._stop = threading.Event()
        self._server = None
        self._threads = []

    # ---------- Вхід із приймача ----------

    def wants(self, wants):
        """``wants(mode)`` — той самий предикат, що для режимів Telegram."""
        return bool(wants(self.mode))

    def publish(self, pcm, caption, *, clip_id=None, at=None, play=True):
        if self._stop.is_set():
            return None
        pcm = bytes(pcm)
        if clip_id is None:
            clip_id = next(self._ids)
        with self._history_lock:
            self._history.append({
                "id": clip_id,
                "at": time.time() if at is None else at,
                "caption": caption,
                "duration_s": round(len(pcm) / 2 / self.sample_rate, 1),
                "pcm": pcm,
            })
        if play:
            self.mixer.add(pcm)
        return clip_id

    def reset_history(self, session):
        """Нумерація належить іншому джерелу (server/phrase_feed.py)."""
        with self._history_lock:
            self._history.clear()
            self.session = session

    def live_audio(self, pcm, phase="frame"):
        """Кадри мовлення з VAD у реальному часі; викликається з потоку захоплення.

        ``phase``: start (preroll + перший кадр), frame, end (останній кадр сегмента).
        Сира копія йде одразу; оцінка сигналів — в окремому потоці, щоб не гальмувати захоплення.
        """
        if self._stop.is_set():
            return
        self._air_at = float("-inf") if phase == "end" else time.monotonic()
        self.channels["live"].mixer.add(pcm)
        try:
            self._gate_queue.put_nowait((pcm, phase))
        except queue.Full:
            runtime_count("radio_stream.gate_overflow")

    def features(self):
        """Що вміє саме цей вузол: застосунок ховає недоступне, а не вгадує за адресою."""
        return ["live", "live_clean", "selected", "events", "clips", *(("transmit",) if self.transmit_enabled else ())]

    def on_air(self):
        """Зараз іде мовлення; без кінця сегмента згасає само."""
        return time.monotonic() - self._air_at < 1.5

    def input_muted(self):
        """Напівдуплекс: True під час передачі з iPhone і TX_TAIL_S після неї."""
        return self._tx_active or time.monotonic() < self._tx_until

    def transmit_session(self, frames, log_name=""):
        """Відтворює PCM із ``frames`` (ітератор bytes). Повертає (секунд, байт, причина)."""
        if not self._tx_lock.acquire(blocking=False):
            raise RuntimeError("busy")
        started, total, reason = time.monotonic(), 0, "кінець"
        self._tx_active = True
        key = self.ptt_key
        try:
            with self.player_factory(self.sample_rate) as play:
                if key is not None:
                    key.press()
                    if self.ptt_lead_s:
                        time.sleep(self.ptt_lead_s)  # кадри тим часом чекають у сокеті
                for pcm in frames:
                    if time.monotonic() - started > TX_MAX_S:
                        reason = f"ліміт {TX_MAX_S:.0f} с"
                        break
                    if pcm:
                        play(pcm)
                        total += len(pcm)
                if key is not None and self.ptt_tail_s:
                    time.sleep(self.ptt_tail_s)
        finally:
            try:
                if key is not None:
                    key.release()
            finally:
                self._tx_until = time.monotonic() + TX_TAIL_S
                self._tx_active = False
                self._tx_lock.release()
        return time.monotonic() - started, total, reason

    def set_signal_matcher(self, matcher, mode_getter):
        """Фільтр сигналів приймача для каналу «наживо без сигналів»; режим — як у приймачі."""
        self.gate.matcher = matcher
        self.gate.mode_getter = mode_getter

    def _gate_loop(self):
        while not self._stop.is_set():
            try:
                pcm, phase = self._gate_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.gate.feed(pcm, phase)
            except Exception:
                runtime_count("radio_stream.gate_error")
                if self.log:
                    self.log.exception("Потік для iPhone: помилка відсіювання сигналів")

    def history(self, after=0):
        with self._history_lock:
            return [
                {k: v for k, v in item.items() if k != "pcm"}
                for item in self._history if item["id"] > after
            ]

    def clip_pcm(self, clip_id):
        with self._history_lock:
            for item in self._history:
                if item["id"] == clip_id:
                    return item["pcm"]
        return None

    @property
    def listeners(self):
        return sum(c.listeners for c in self.channels.values())

    def encode_clip(self, pcm):
        return subprocess.run(encoder_cmd(self.sample_rate, live=False), input=pcm,
                              capture_output=True, check=True, timeout=30).stdout

    # ---------- HTTP ----------

    def authorized(self, address):
        return client_allowed(address) and self.peers.is_owner(address)

    def start(self):
        self._server = _QuietServer((self.host, self.port), _handler_for(self))
        self._server.daemon_threads = True
        for channel in self.channels.values():
            channel.start(self._stop, self._threads)
        for target, name in ((self._server.serve_forever, "radio-stream-http"),
                             (self._gate_loop, "radio-stream-gate")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for channel in self.channels.values():
            channel.stop()
        if self.ptt_key is not None:
            try:
                self.ptt_key.close()
            except Exception:
                pass
        close = getattr(self.player_factory, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


def _handler_for(stream):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Radio345Stream"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="text/plain; charset=utf-8", extra=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Radio345-Api", str(API_VERSION))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not stream.authorized(self.client_address[0]):
                return self._send(403, b"not your tailscale device")
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/version":
                body = json.dumps({"api": API_VERSION, "supported": list(API_SUPPORTED),
                                   "features": stream.features()}).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            wanted = (query.get("api") or [self.headers.get("X-Radio345-Api") or ""])[0]
            if wanted and wanted not in {str(v) for v in API_SUPPORTED}:
                body = json.dumps({"error": "unsupported_api", "api": API_VERSION,
                                   "supported": list(API_SUPPORTED)}).encode()
                return self._send(426, body, "application/json; charset=utf-8")
            if url.path == "/stream.mp3":
                return self._stream(stream.channels["selected"])
            if url.path == "/live.mp3":
                return self._stream(stream.channels["live"])
            if url.path == "/transmit":
                return self._transmit()
            if url.path == "/live-clean.mp3":
                return self._stream(stream.channels["live_clean"])
            if url.path == "/events":
                try:
                    after = int((query.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                body = json.dumps({
                    "clips": stream.history(after),
                    "session": stream.session,
                    "listeners": stream.listeners,
                    "live_listeners": stream.channels["live"].listeners + stream.channels["live_clean"].listeners,
                    "at_home": stream.peers.is_home(self.client_address[0]),
                    "signals_dropped": stream.gate.dropped,
                    "transmit": stream.transmit_enabled,
                    "transmitting": stream._tx_active,
                    "air": stream.on_air(),
                    **({"level": stream.level.read()} if getattr(stream, "level", None) is not None else {}),
                    "ptt_key": stream.ptt_key.name if stream.ptt_key is not None else "немає",
                    "pending_s": round(stream.mixer.pending_s, 1),
                    "mode": stream.mode,
                }, ensure_ascii=False).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            if url.path.startswith("/clip/") and url.path.endswith(".mp3"):
                try:
                    pcm = stream.clip_pcm(int(url.path[6:-4]))
                except ValueError:
                    pcm = None
                if pcm is None:
                    return self._send(404, b"not found")
                try:
                    body = stream.encode_clip(pcm)
                except (OSError, subprocess.SubprocessError):
                    return self._send(500, b"encode failed")
                return self._send(200, body, "audio/mpeg")
            return self._send(404, b"not found")

        def _transmit(self):
            if not stream.transmit_enabled:
                return self._send(403, b"transmit disabled")
            key = self.headers.get("Sec-WebSocket-Key")
            if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
                return self._send(400, b"websocket required")
            if stream._tx_active:
                return self._send(409, b"busy")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(TX_IDLE_S)
            state = {"reason": None}

            def frames():
                while True:
                    try:
                        opcode, payload = ws_read_frame(self.rfile)
                    except (TimeoutError, OSError, ValueError) as e:
                        state["reason"] = f"обрив ({type(e).__name__})"
                        return
                    if opcode is None or opcode == 0x8:
                        state["reason"] = "кнопку відпущено"
                        try:
                            self.wfile.write(ws_frame(0x8, b"\x03\xe8"))
                        except OSError:
                            pass
                        return
                    if opcode == 0x9:
                        self.wfile.write(ws_frame(0xA, payload))
                    elif opcode == 0x2:
                        yield payload

            try:
                seconds, total, reason = stream.transmit_session(frames())
            except RuntimeError:
                return
            except Exception:
                if stream.log:
                    stream.log.exception("Потік для iPhone: помилка передачі")
                return
            if stream.log:
                stream.log.info("Потік для iPhone: передача з %s — %.1f с, %.1f с звуку; %s",
                                self.client_address[0], seconds, total / 2 / stream.sample_rate,
                                state["reason"] or reason)

        def _stream(self, channel):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            q = channel.add_client()
            started, sent, reason = time.monotonic(), 0, "stop"
            if stream.log:
                stream.log.info("Потік для iPhone: підключено %s до %s (Range=%s, UA=%s)",
                                self.client_address[0], channel.name, self.headers.get("Range", "-"),
                                (self.headers.get("User-Agent") or "-")[:80])
            try:
                while True:
                    try:
                        data = q.get(timeout=5)
                    except queue.Empty:
                        if channel.has_client(q):
                            continue
                        data = None
                    if data is None:
                        reason = "сервер: повільний клієнт" if not stream._stop.is_set() else "сервер зупиняється"
                        return
                    self.wfile.write(data)
                    sent += len(data)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                reason = f"клієнт закрив ({type(e).__name__})"
            finally:
                channel.drop_client(q)
                if stream.log:
                    stream.log.info("Потік для iPhone: відключено %s від %s через %.1f с, %d байт; %s",
                                    self.client_address[0], channel.name, time.monotonic() - started, sent, reason)

    return Handler


def start_radio_stream(config, log, force=False):
    """Запускає потік, якщо його ввімкнено; інакше чи при помилці — None."""
    env = os.environ.get("RADIO_STREAM", "").strip().lower()
    enabled = env in ("1", "true", "yes", "on") or (
        env == "" and getattr(config, "RADIO_STREAM_ENABLED", False))
    if not (enabled or force):
        return None
    mode = os.environ.get("RADIO_STREAM_MODE", "").strip() or config.RADIO_STREAM_MODE
    if not shutil.which("ffmpeg"):
        log.warning("Потік для iPhone вимкнено: немає ffmpeg")
        return None
    host = getattr(config, "RADIO_STREAM_HOST", "") or tailscale_ipv4()
    if not host:
        log.warning("Потік для iPhone не запущено: Tailscale не активний (немає адреси 100.x)")
        return None
    try:
        if ipaddress.ip_address(host).is_unspecified:
            log.error("Потік для iPhone: адреса 0.0.0.0/:: заборонена, вкажіть конкретну")
            return None
    except ValueError:
        log.error("Потік для iPhone: некоректна адреса %s", host)
        return None
    from shared.audio_devices import NULL_OUTPUT, configured
    from shared.ptt_key import GuardedKey, make_key
    transmit = getattr(config, "RADIO_STREAM_TRANSMIT", False)
    output = configured("RADIO_OUTPUT_DEVICE", config)
    muted = output.casefold() == NULL_OUTPUT
    key = None
    try:
        if transmit:
            # Без звуку ключ не натискаємо: рація передавала б порожню несну.
            spec = "none" if muted else configured("RADIO_PTT", config) or "none"
            key = GuardedKey(make_key(spec), TX_MAX_S + 5, log)
        stream = RadioStream(
            sample_rate=config.SR, host=host, port=config.RADIO_STREAM_PORT,
            mode=mode, log=log, transmit=transmit,
            player_factory=NullOutput() if muted else SharedSpeaker(device=output),
            ptt_key=key,
            ptt_lead_s=getattr(config, "RADIO_PTT_LEAD_MS", 0) / 1000,
            ptt_tail_s=getattr(config, "RADIO_PTT_TAIL_MS", 0) / 1000,
        ).start()
    except (OSError, ValueError, ImportError) as e:
        if key is not None:
            key.close()
        log.error("Потік для iPhone не запущено: %s", e, extra={"console": True})
        return None
    if transmit:
        log.info("Передача з iPhone: вихід %s, ключ PTT — %s",
                 "у порожнечу" if muted else output or "системний", key.name, extra={"console": True})
    log.info("Потік для iPhone: %s:%s (режим %s; доступ — ваші пристрої в Tailscale)",
             host, config.RADIO_STREAM_PORT, mode, extra={"console": True})
    return stream
