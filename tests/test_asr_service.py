"""Черга Pi: фрази не губляться й не дублюються, старі не стають подіями."""
import logging
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core.segment_timing import SegmentTiming, TimedSegment
from core.transcriber import Transcriber
from shared.whisper_result import RecognizerUnavailable
from services.asr_service import AsrService, NullTelegram, SpoolRecorder
from shared.recordings import RecordingStore
from services.segment_spool import SegmentSpool

LOG = logging.getLogger("test")


def phrase(age=0.0, amp=1500):
    return TimedSegment(np.full(16000, amp, dtype=np.int16).tobytes(), SegmentTiming(
        enqueued_at=0.0, enqueued_unix_s=time.time(), audio_end_unix_s=time.time() - age))


class Remote:
    prompt = ""
    up = True
    calls = 0

    def transcribe(self, audio, prompt=None):
        self.calls += 1
        if not self.up:
            raise RecognizerUnavailable("спить")
        return {"segments": [{"text": " клен", "compression_ratio": 1.0}]}

    extract_text = staticmethod(lambda r: r["segments"][0]["text"].strip())


class Events:
    callsigns = commands = None

    def __init__(self):
        self.live = []

    def annotate(self, text):
        return SimpleNamespace(text=text, callsigns=(), commands=(), watch=(), recipient="", sender="", rejected=False)

    def process(self, text, pcm, raw_text=None):
        self.live.append(text)


@pytest.fixture
def system(tmp_path):
    store = RecordingStore(tmp_path / "rec")
    recorder, remote, events = SpoolRecorder(store, {}), Remote(), Events()
    worker = Transcriber(segments=None, recognizer=remote, process_text=lambda t: t, event_processor=events,
                         sample_rate=16000, min_rms=0, normalize_audio=False, debug_rms=False, blacklist=(),
                         log=LOG, recorder=recorder)
    worker.deferred = store
    spool = SegmentSpool(tmp_path / "spool")
    return SimpleNamespace(store=store, remote=remote, events=events, worker=worker, spool=spool,
                           service=AsrService(spool, worker, recorder, LOG), recorder=recorder)


def rows(store):
    with store.connect() as db:
        return [(r["id"], r["status"]) for r in db.execute("SELECT id,status FROM recordings")]


def test_fresh_phrase_becomes_one_event_and_one_record(system):
    system.spool.put(phrase(), 16000)
    assert system.service.step() and system.events.live == ["клен"]
    assert len(rows(system.store)) == 1 and not system.spool.files()


def test_mac_asleep_then_awake(system):
    system.remote.up = False
    for _ in range(3):
        system.spool.put(phrase(), 16000)
    for _ in range(3):
        assert system.service.step()
    assert not system.spool.files() and [s for _, s in rows(system.store)] == ["deferred"] * 3
    assert system.events.live == []
    system.remote.up = True
    for _ in range(4):
        system.worker._deferred_idle_until = 0
        system.service.step()
    assert len(rows(system.store)) == 3 and all(s != "deferred" for _, s in rows(system.store))
    assert system.events.live == [], "догнане не створює подій"


def test_old_phrase_is_archived_without_events(system):
    system.spool.put(phrase(age=4000), 16000)
    system.service.step()
    assert [s for _, s in rows(system.store)] == ["deferred"] and system.remote.calls == 0


def test_crash_after_archive_before_delete_does_not_duplicate(system):
    system.spool.put(phrase(), 16000)
    path = system.spool.files()[0]
    saved = path.read_bytes()
    system.service.step()
    path.write_bytes(saved)
    system.service.step()
    assert len(rows(system.store)) == 1 and system.events.live == ["клен"] and not system.spool.files()


def test_archive_failure_keeps_phrase_and_retries_without_second_event(system, monkeypatch):
    system.spool.put(phrase(), 16000)
    real = system.store.save
    monkeypatch.setattr(system.store, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("диск")))
    assert system.service.step() is False and len(system.spool.files()) == 1
    monkeypatch.setattr(system.store, "save", real)
    assert system.service.step()
    assert len(rows(system.store)) == 1 and system.events.live == ["клен"] and not system.spool.files()


def test_damaged_and_partial_files_do_not_block(system):
    (system.spool.root / "000000000000001-aa.seg").write_bytes(b"\x01")
    (system.spool.root / "000000000000002-bb.tmp").write_bytes(b"\x01")
    assert system.service.step()
    assert not system.spool.files() and list(system.spool.root.glob("*.bad"))


def test_fresh_first_and_limit_drops_oldest(tmp_path):
    spool = SegmentSpool(tmp_path / "s", max_bytes=70_000)
    for age in (30, 20, 10):
        spool.put(phrase(age=age), 16000)
    assert len(spool.files()) == 2
    assert spool.read(spool.files()[-1])[3] > spool.read(spool.files()[0])[3]


def test_telegram_is_never_used():
    assert NullTelegram().recipients_for(lambda mode: True) == []


def test_bot_listener_is_restarted_after_it_dies():
    from services.asr_service import Bot
    started = []
    service = SimpleNamespace(start_maintenance=lambda: None, callback_listener=lambda: started.append(1))
    bot = Bot(service, LOG)
    bot.tick()
    bot.thread.join(2)
    bot.tick()
    assert len(started) == 1, "не частіше ніж раз на 10 с"
    bot.next_start = 0
    bot.tick()
    bot.thread.join(2)
    assert len(started) == 2
