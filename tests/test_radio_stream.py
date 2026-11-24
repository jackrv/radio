import json
import shutil
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared import radio_stream as rs

SR = 16000


def tone(seconds, value=1000):
    return int(value).to_bytes(2, "little", signed=True) * int(SR * seconds)


def test_mixer_plays_clips_then_silence_and_drops_oldest_backlog(monkeypatch):
    monkeypatch.setattr(rs, "MAX_PENDING_S", 0.25)
    mixer = rs.ClipMixer(SR)
    assert mixer.next_chunk() == bytes(mixer.chunk_bytes)
    mixer.add(tone(0.15, 1))
    mixer.add(tone(0.15, 2))  # разом 0.3 с > 0.25 с: найстаріша фраза відкидається
    assert mixer.dropped == 1
    first = mixer.next_chunk()
    assert first == tone(0.1, 2)
    second = mixer.next_chunk()
    assert second[: len(tone(0.05))] == tone(0.05, 2)
    assert second[len(tone(0.05)):] == bytes(len(tone(0.05)))


@pytest.mark.parametrize("address, allowed", [
    ("127.0.0.1", True), ("::1", True), ("100.101.102.103", True),
    ("fd7a:115c:a1e0::1", True), ("::ffff:100.64.0.5", True),
    ("192.168.1.10", False), ("8.8.8.8", False), ("junk", False),
])
def test_only_loopback_and_tailnet_clients(address, allowed):
    assert rs.client_allowed(address) is allowed


def test_start_refuses_unspecified_host_and_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("RADIO_STREAM", raising=False)
    log = Mock()
    cfg = SimpleNamespace(RADIO_STREAM_ENABLED=False, RADIO_STREAM_HOST="0.0.0.0", RADIO_STREAM_PORT=0,
                          RADIO_STREAM_MODE="watch", SR=SR)
    assert rs.start_radio_stream(cfg, log) is None
    monkeypatch.setenv("RADIO_STREAM", "1")
    monkeypatch.setattr(rs.shutil, "which", lambda name: "/usr/bin/" + name)
    assert rs.start_radio_stream(cfg, log) is None
    assert log.error.called
    assert not (tmp_path / "t").exists()


def make_processor(stream, select=lambda _: []):
    from shared.callsigns import Callsigns, command_dictionary
    from server.event_processor import EventProcessor
    return EventProcessor(
        callsigns=Callsigns.from_lines([]), commands=command_dictionary(["грім 022"]),
        watch=Callsigns.from_lines([]), short_garbage=set(), save_event=Mock(return_value=1),
        telegram_service=Mock(), fresh=lambda _: True, select_recipients=select,
        db_path="unused.db", log_callsigns_only=True, log=Mock(), radio_stream=stream,
    )


def test_processor_publishes_by_mode_without_telegram_recipients():
    stream = Mock(mode="commands")
    stream.wants.side_effect = lambda wants: wants(stream.mode)
    make_processor(stream).process("грім 022 прийом", b"\1\0" * 10)
    pcm, caption = stream.publish.call_args.args
    assert pcm == b"\1\0" * 10
    assert caption.startswith("Подія: Ефір") and "ГРІМ" in caption

    quiet = Mock(mode="watch")
    quiet.wants.side_effect = lambda wants: wants(quiet.mode)
    make_processor(quiet).process("грім 022 прийом", b"\0\0")
    assert not quiet.publish.called


def test_stream_failure_does_not_block_telegram():
    stream = Mock()
    stream.wants.side_effect = RuntimeError("synthetic")
    recipients = [{"chat_id": 1, "role": "admin", "mode": "all"}]
    processor = make_processor(stream, select=lambda _: recipients)
    processor.process("грім 022 прийом", b"\0\0")
    assert processor.telegram_service.notify.called


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg недоступний")
def test_http_stream_events_and_clip_on_loopback():
    stream = rs.RadioStream(sample_rate=SR, host="127.0.0.1", port=0).start()
    try:
        base = "http://127.0.0.1:%d" % stream._server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/", timeout=5)
        assert missing.value.code == 404  # сторінки для Safari більше немає
        stream.peers.is_owner = lambda address: False
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(base + "/events", timeout=5)
        assert denied.value.code == 403
        stream.peers.is_owner = lambda address: True

        clip_id = stream.publish(tone(0.5), "Подія: Ефір\n\nсинтетична фраза")
        events = json.load(urllib.request.urlopen(base + "/events", timeout=5))
        assert [c["caption"] for c in events["clips"]] == ["Подія: Ефір\n\nсинтетична фраза"]
        assert "pcm" not in events["clips"][0]
        clip = urllib.request.urlopen(base + f"/clip/{clip_id}.mp3", timeout=10).read()
        assert len(clip) > 500

        live = urllib.request.urlopen(base + "/stream.mp3", timeout=10)
        assert live.headers["Content-Type"] == "audio/mpeg"
        data, deadline = b"", time.monotonic() + 8
        while len(data) < 2000 and time.monotonic() < deadline:
            data += live.read1(4096)
        assert len(data) >= 2000
        assert stream.listeners == 1
        live.close()
    finally:
        stream.stop()


def test_supervisor_feeds_live_speech_with_preroll_and_isolates_errors():
    from server.supervisor import ApplicationSupervisor
    sent = []
    sup = ApplicationSupervisor.__new__(ApplicationSupervisor)
    sup.on_live_audio = lambda pcm, phase: sent.append((pcm, phase))
    seg = SimpleNamespace(in_speech=False, voiced=[])
    sup._feed_live(b"a", False, seg)                       # тиша — нічого
    seg.in_speech, seg.voiced = True, [b"p1", b"p2", b"s"]
    sup._feed_live(b"s", False, seg)                       # початок: preroll + кадр
    sup._feed_live(b"t", True, seg)                        # усередині
    seg.in_speech = False
    sup._feed_live(b"u", True, seg)                        # останній кадр сегмента
    sup._feed_live(b"v", False, seg)
    assert sent == [(b"p1p2s", "start"), (b"t", "frame"), (b"u", "end")]
    sup.on_live_audio = Mock(side_effect=RuntimeError("synthetic"))
    seg.in_speech = True
    sup._feed_live(b"w", True, seg)                        # не піднімає виняток


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg недоступний")
def test_live_channel_streams_separately():
    stream = rs.RadioStream(sample_rate=SR, host="127.0.0.1", port=0).start()
    try:
        base = "http://127.0.0.1:%d" % stream._server.server_address[1]
        live = urllib.request.urlopen(base + "/live.mp3", timeout=10)
        stream.live_audio(tone(0.3))
        data, deadline = b"", time.monotonic() + 8
        while len(data) < 2000 and time.monotonic() < deadline:
            data += live.read1(4096)
        assert len(data) >= 2000
        events = json.load(urllib.request.urlopen(base + "/events", timeout=5))
        assert events["live_listeners"] == 1 and events["clips"] == []
        live.close()
    finally:
        stream.stop()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg недоступний")
def test_new_listener_gets_burst_of_recent_audio():
    stream = rs.RadioStream(sample_rate=SR, host="127.0.0.1", port=0).start()
    try:
        time.sleep(2.0)  # накопичити запас
        base = "http://127.0.0.1:%d" % stream._server.server_address[1]
        live = urllib.request.urlopen(base + "/live.mp3", timeout=10)
        started = time.monotonic()
        data = b""
        while len(data) < int(rs.BYTES_PER_S * 1.2) and time.monotonic() - started < 5:
            data += live.read1(4096)
        # Запас приходить одразу, а не за реальний час (~1,2 с).
        assert time.monotonic() - started < 0.5
        live.close()
    finally:
        stream.stop()


class FakeMatcher:
    def __init__(self, candidate, frames=100):
        self.templates = [SimpleNamespace(frames=frames)]  # 100 × 10 мс = 1 с
        self.max_outside_s = 0.6
        self.candidate = candidate

    def score_pcm(self, pcm):
        return {"score": 0.9, "outside_s": 0.1, "extra_ratio": 0.0, "candidate": self.candidate}

    def is_candidate(self, score, outside, extra):
        return self.candidate


def test_signal_gate_drops_short_signal_and_releases_speech():
    out = []
    gate = rs.SignalGate(SR, out.append, FakeMatcher(candidate=True), lambda: "active")
    assert gate.hold_s == pytest.approx(0.5 + 1.0 + 0.6 + 0.2)
    gate.feed(tone(0.5), "start"); gate.feed(tone(0.03), "frame"); gate.feed(tone(0.03), "end")
    assert out == [] and gate.dropped == 1

    gate.matcher.candidate = False
    gate.feed(tone(0.5), "start"); gate.feed(tone(0.03), "end")
    assert len(out) == 1 and len(out[0]) == len(tone(0.53))

    # Довше за hold_s — відпускаємо без оцінки, далі кадри йдуть одразу.
    out.clear(); gate.matcher.candidate = True
    gate.feed(tone(0.5), "start")
    for _ in range(60):
        gate.feed(tone(0.03), "frame")   # 0,5 + 1,8 = 2,3 с — ще притримується
    assert out == []
    gate.feed(tone(0.03), "frame")       # понад hold_s — відпущено разом
    assert len(out) == 1 and len(out[0]) == len(tone(0.5)) + 61 * len(tone(0.03))
    gate.feed(tone(0.03), "frame"); gate.feed(tone(0.03), "end")
    assert len(out) == 3 and gate.dropped == 1


def test_signal_gate_passes_through_when_filter_off():
    out = []
    gate = rs.SignalGate(SR, out.append, FakeMatcher(candidate=True), lambda: "off")
    gate.feed(b"ab", "start"); gate.feed(b"cd", "end")
    assert out == [b"ab", b"cd"]


def _status():
    return {
        "Self": {"UserID": 7, "TailscaleIPs": ["100.64.0.1"]},
        "Peer": {
            "a": {"UserID": 7, "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"], "CurAddr": "192.168.1.20:41641"},
            "b": {"UserID": 7, "TailscaleIPs": ["100.64.0.3"], "CurAddr": "8.8.8.8:41641"},
            "c": {"UserID": 9, "TailscaleIPs": ["100.64.0.4"], "CurAddr": "192.168.1.21:41641"},
        },
    }


def test_tailnet_peers_home_uses_direct_private_path():
    peers = rs.TailnetPeers(_status)
    assert peers.is_home("100.64.0.2") is True
    assert peers.is_home("100.64.0.3") is False
    assert peers.is_home("100.64.0.9") is False
    broken = rs.TailnetPeers(lambda: (_ for _ in ()).throw(OSError("synthetic")))
    assert broken.is_home("100.64.0.2") is False


def test_tailnet_peers_owner_only_same_account():
    peers = rs.TailnetPeers(_status)
    assert peers.is_owner("100.64.0.2") is True
    assert peers.is_owner("fd7a:115c:a1e0::2") is True
    assert peers.is_owner("::ffff:100.64.0.3") is True
    assert peers.is_owner("100.64.0.1") is True        # сам вузол
    assert peers.is_owner("127.0.0.1") is True
    assert peers.is_owner("100.64.0.4") is False       # пристрій іншого облікового запису (shared)
    assert peers.is_owner("100.64.0.9") is False
    assert peers.is_owner("junk") is False
    assert rs.TailnetPeers(lambda: {}).is_owner("100.64.0.2") is False
    broken = rs.TailnetPeers(lambda: (_ for _ in ()).throw(OSError("synthetic")))
    assert broken.is_owner("100.64.0.2") is False


def test_tailnet_peers_refreshes_unknown_address_rate_limited(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock[0])
    calls, status = [], {"Self": {"UserID": 7}, "Peer": {}}
    peers = rs.TailnetPeers(lambda: calls.append(1) or status)
    assert peers.is_owner("100.64.0.2") is False
    status = {"Self": {"UserID": 7}, "Peer": {"a": {"UserID": 7, "TailscaleIPs": ["100.64.0.2"]}}}
    clock[0] += 1
    assert peers.is_owner("100.64.0.2") is False and len(calls) == 1   # не частіше ніж раз на 2 с
    clock[0] += 2
    assert peers.is_owner("100.64.0.2") is True and len(calls) == 2
    assert peers.is_owner("100.64.0.2") is True and len(calls) == 2    # відомий — з кешу


def test_unknown_self_account_denies_everyone():
    peers = rs.TailnetPeers(lambda: {"Peer": {"a": {"TailscaleIPs": ["100.64.0.2"]}}})
    assert peers.is_owner("100.64.0.2") is False


def _ws_client_frame(opcode, payload, mask=b"\x01\x02\x03\x04"):
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    else:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg недоступний")
def test_transmit_websocket_plays_pcm_and_mutes_receiver_input():
    import socket
    played = []

    class FakePlayer:
        def __init__(self, sample_rate):
            pass
        def __enter__(self):
            return played.append
        def __exit__(self, *exc):
            return False

    stream = rs.RadioStream(sample_rate=SR, host="127.0.0.1", port=0,
                            transmit=True, player_factory=FakePlayer).start()
    try:
        port = stream._server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(("GET /transmit HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        reply = sock.recv(1024).decode()
        assert reply.startswith("HTTP/1.1 101") and "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in reply
        chunk = tone(0.1, 500)
        sock.sendall(_ws_client_frame(0x2, chunk) * 3)
        deadline = time.monotonic() + 3
        while len(played) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert played == [chunk] * 3
        assert stream.input_muted()
        sock.sendall(_ws_client_frame(0x8, b"\x03\xe8"))
        sock.recv(16)
        sock.close()
        deadline = time.monotonic() + 3
        while stream._tx_active and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stream.input_muted()          # хвіст TX_TAIL_S
        time.sleep(rs.TX_TAIL_S + 0.1)
        assert not stream.input_muted()
    finally:
        stream.stop()


def test_transmit_disabled_by_default_and_supervisor_drops_muted_input():
    stream = rs.RadioStream(sample_rate=SR, host="127.0.0.1", port=0)
    assert stream.transmit_enabled is False and stream.input_muted() is False
    from server.supervisor import ApplicationSupervisor
    sup = ApplicationSupervisor.__new__(ApplicationSupervisor)
    sup.input_muted = Mock(side_effect=RuntimeError("synthetic"))
    assert sup._input_is_muted() is False


def test_shared_speaker_stays_open_between_sessions(monkeypatch):
    opened = []

    class FakeOut:
        def __init__(self, **kw):
            opened.append(self)
            self.closed = False
        def start(self): pass
        def write(self, data): pass
        def stop(self): pass
        def close(self): self.closed = True

    import types
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", types.SimpleNamespace(RawOutputStream=FakeOut))
    speaker = rs.SharedSpeaker(idle_s=0.2)
    with speaker(SR) as play:
        play(b"\0\0")
    with speaker(SR) as play:
        play(b"\0\0")
    assert len(opened) == 1 and not opened[0].closed
    time.sleep(0.4)
    assert opened[0].closed and speaker.out is None
