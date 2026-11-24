"""Жива схема: знімок без текстів, зупинка без глядача, вибір дому."""
import json
import sqlite3
import time

from server import flow as f
from server.asr_service import whisper_state
from shared import home_marker


def wait(check):
    end = time.monotonic() + 10
    while not check():
        assert time.monotonic() < end
        time.sleep(0.05)


def test_probe_reports_counters_only(tmp_path):
    data = tmp_path / ".local-data"
    (data / "segment-spool").mkdir(parents=True)
    (data / "segment-spool/1-a.seg").write_bytes(b"x")
    (data / "node").mkdir()
    (data / "node/state.json").write_text(json.dumps({"state": "listening", "at": time.time()}))
    report = whisper_state(data / "node/whisper.json")
    report("sending")
    report("ready")
    db = sqlite3.connect(data / "events.db")
    db.execute("CREATE TABLE radio_events (id INTEGER PRIMARY KEY, text TEXT)")
    db.execute("INSERT INTO radio_events VALUES (7, 'синтетичний текст')")
    db.commit()
    db.close()
    flow = f.Flow(tmp_path)
    try:
        wait(lambda: flow.read()["data"])
        got = flow.read()["data"]
    finally:
        with flow.lock:
            flow._stop()
    assert (got["node"], got["queue"], got["segment"], got["event"], got["sent"]) == ("listening", 1, "1-a.seg", 7, None)
    assert (got["whisper"]["state"], got["whisper"]["sent"]) == ("ready", 1) and got["whisper"]["age_s"] < 10
    assert "синтетичний" not in json.dumps(got, ensure_ascii=False)


def test_probe_stops_when_nobody_watches(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "IDLE_S", 0.0)
    monkeypatch.setattr(f, "PROBE", "import time\nfor n in range(99):\n print('{}', flush=True); time.sleep(.1)\n")
    flow = f.Flow(tmp_path)
    flow.read()
    process = flow._process
    wait(lambda: flow._process is None)
    wait(lambda: process.poll() is not None)


def test_home_elsewhere_goes_over_ssh_or_nowhere(tmp_path, monkeypatch):
    (tmp_path / ".local-data").mkdir()
    home_marker.write(tmp_path, "pi", 1)
    monkeypatch.setattr(home_marker, "this_device", lambda: "mac")
    calls = []
    def popen(command, **kwargs):
        calls.append(command)
        raise OSError
    assert f.Flow(tmp_path, popen=popen).read() == {"home": None, "data": None, "age_s": None}
    assert calls == []
    flow = f.Flow(tmp_path, host="user@example", popen=popen)
    assert flow.read()["home"] == "pi" and calls[0][0] == "ssh"
    flow.read()
    assert len(calls) == 1, "повтор — не частіше RETRY_S"


def test_stream_reports_speech_until_segment_ends():
    from shared.radio_stream import RadioStream
    stream = RadioStream(sample_rate=16000, host="127.0.0.1", port=0)
    assert not stream.on_air()
    stream.live_audio(b"\0\0" * 480, "start")
    assert stream.on_air()
    stream.live_audio(b"\0\0" * 480, "end")
    assert not stream.on_air()


def test_level_meter_matches_the_threshold_metric():
    import numpy as np
    from server.level_meter import LevelMeter
    from shared.audio import prepare_audio
    now = [0.0]
    meter = LevelMeter(clock=lambda: now[0])
    assert meter.read()["rms"] is None
    loud, quiet = np.full(480, 3277, np.int16).tobytes(), np.full(480, 327, np.int16).tobytes()
    meter.add(loud)
    meter.segment(loud)
    assert meter.read()["phrase_rms"] == round(prepare_audio(loud, 0, False)[1], 4) == meter.read()["rms"]
    now[0] = 1.5   # гучне вийшло з вікна
    meter.add(quiet)
    assert meter.read()["rms"] == round(327 / 32768, 4) and meter.read()["phrase_age_s"] == 1.5
    now[0] = 9.0
    assert meter.read()["rms"] is None, "мікрофон закрито"
    meter.add(b"x")   # непарні байти не валять захоплення


def test_last_passed_phrase_survives_later_quiet_ones():
    import numpy as np
    from server.level_meter import LevelMeter
    now = [0.0]
    meter = LevelMeter(clock=lambda: now[0])
    sizes = (2000, 900, 1500, 300, 200, 250)
    for n, value in enumerate(sizes):
        now[0] = float(n)
        meter.segment(np.full(480, value, np.int16).tobytes())
    louder = meter.read()["louder"]
    for threshold in (0.0, 0.0062, 0.009, 0.03, 0.05):
        want = [n for n, v in enumerate(sizes) if v / 32768 >= threshold][-1]
        assert [r for r in louder if r[0] >= threshold][-1][1] == now[0] - want
