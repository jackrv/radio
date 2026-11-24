"""Тексти з Pi: розпізнавання → файли → ефір, без повторів після перезапусків."""
import logging
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import phrase_feed as f
from shared import radio_stream as rs

LOG = logging.getLogger("test")
PCM = b"\x01\x00" * 1600


def stream():
    s = rs.RadioStream(sample_rate=16000, host="127.0.0.1", port=0, peers=Mock(), player_factory=Mock())
    s.channels["selected"].mixer = s.mixer = Mock()
    return s


def outbox(path, **kw):
    return f.PhraseOutbox(path, mode="watch", sample_rate=16000, **kw)


def ids(s):
    return [c["id"] for c in s.history()]


def test_new_phrase_reaches_stream_with_caption_time_and_sound(tmp_path):
    box, s = outbox(tmp_path / "p", current=lambda: ("rec1", 123.0)), stream()
    inbox = f.PhraseInbox(tmp_path / "p", s)
    inbox.step()
    assert box.publish(PCM, "Подія: Ефір\n\nсинтетична") == 1
    inbox.step(); inbox.step()
    assert s.history() == [{"id": 1, "at": 123.0, "caption": "Подія: Ефір\n\nсинтетична", "duration_s": 0.1}]
    assert s.clip_pcm(1) == PCM and s.mixer.add.call_count == 1


def test_node_restart_keeps_ids_and_session_without_sound(tmp_path):
    box = outbox(tmp_path)
    for _ in range(3):
        box.publish(PCM, "а")
    first, second = stream(), stream()
    for s in (first, second):
        f.PhraseInbox(tmp_path, s).step()
        assert ids(s) == [1, 2, 3]
        s.mixer.add.assert_not_called()
    assert first.session == second.session


def test_asr_restart_continues_numbering(tmp_path):
    outbox(tmp_path).publish(PCM, "а")
    s = stream()
    inbox = f.PhraseInbox(tmp_path, s)
    inbox.step()
    session = s.session
    assert outbox(tmp_path).publish(PCM, "б") == 2
    inbox.step()
    assert ids(s) == [1, 2] and s.session == session


def test_same_record_is_published_once(tmp_path):
    for _ in range(2):   # збій до запису в архів, служба перезапустилась
        outbox(tmp_path, current=lambda: ("rec1", 1.0)).publish(PCM, "а")
    assert len(list(tmp_path.glob("*.phr"))) == 1


def test_keeps_only_last_and_files_are_private(tmp_path):
    folder = tmp_path / "p"
    box = outbox(folder, keep=3)
    for _ in range(5):
        box.publish(PCM, "а")
    assert [n for n, _ in f._entries(folder)] == [3, 4, 5]
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert {stat.S_IMODE(p.stat().st_mode) for p in folder.glob("*.phr")} == {0o600}
    assert f.KEEP == rs.HISTORY


@pytest.mark.parametrize("broken", [b"", b"\xff\xff\xff\xff", b"\x02\x00\x00\x00{}ab"])
def test_broken_file_is_skipped_and_next_one_plays(tmp_path, broken):
    box, s = outbox(tmp_path), stream()
    inbox = f.PhraseInbox(tmp_path, s)
    inbox.step()
    (tmp_path / "0000000001-x.phr").write_bytes(broken)
    assert box.publish(PCM, "а") == 2
    inbox.step()
    assert ids(s) == [2] and s.mixer.add.call_count == 1


def test_wiped_folder_starts_new_session(tmp_path):
    outbox(tmp_path).publish(PCM, "а")
    s = stream()
    inbox = f.PhraseInbox(tmp_path, s)
    inbox.step()
    old = s.session
    for path in tmp_path.iterdir():
        path.unlink()
    inbox.step()
    outbox(tmp_path).publish(PCM, "б")
    inbox.step()
    assert s.session != old and [c["caption"] for c in s.history()] == ["б"]


def test_no_asr_service_means_empty_list(tmp_path):
    s = stream()
    f.PhraseInbox(tmp_path / "absent", s).step()
    assert s.history() == []


def test_event_processor_accepts_outbox(tmp_path):
    from core.event_processor import EventProcessor
    box = outbox(tmp_path)
    processor = SimpleNamespace(radio_stream=box, fresh=lambda key: True, log=LOG)
    EventProcessor._publish_stream(processor, lambda mode: mode == "watch", "Тест", "", "", "синтетична", PCM)
    EventProcessor._publish_stream(processor, lambda mode: mode == "all", "Тест", "", "", "ні", PCM)
    s = stream()
    f.PhraseInbox(tmp_path, s).step()
    assert len(ids(s)) == 1 and "синтетична" in s.history()[0]["caption"]


def test_outbox_failure_does_not_stop_recognition(tmp_path, monkeypatch):
    from services.asr_service import phrase_outbox
    monkeypatch.setenv("RADIO_NODE_DIR", str(tmp_path))
    monkeypatch.setenv("RADIO_STREAM_MODE", "невідомий")
    cfg = SimpleNamespace(BASE_DIR=tmp_path, RADIO_STREAM_MODE="watch", SR=16000)
    assert phrase_outbox(cfg, SimpleNamespace(), LOG) is None
    monkeypatch.delenv("RADIO_STREAM_MODE")
    assert phrase_outbox(cfg, SimpleNamespace(record_id=None, captured_at=None), LOG).mode == "watch"
