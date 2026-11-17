"""Словники й налаштування обробки без перезапуску приймача (лише тимчасові бази)."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import callsigns as callsign_module
import config as project_config
import services.text_lists as text_lists
from callsigns import Callsigns, configure_aliases
from core.analyzer import looks_like_garbage
from core.cooldown import Cooldown
from database import init_db as init_dictionary_db
from services.dictionary_loader import LiveDictionaries
from services.event_store import init_db as init_event_db
from services.runtime_settings import (LiveRuntimeSettings, RuntimeSettingsConflict, read_runtime_settings,
                                       write_runtime_settings)
from services.text_lists import read_lists, write_list

ORIGIN = "http://127.0.0.1:8766"
KEY = "x" * 43


@pytest.fixture(autouse=True)
def restore_aliases():
    yield
    configure_aliases(callsign_module.DEFAULT_ALIASES, callsign_module.DEFAULT_CALLSIGN_ALIASES)
    text_lists._synced = None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "live.db"
    init_event_db(path)
    init_dictionary_db(path)
    set_dictionary(path, "callsigns", ["віра: 68"])
    return path


def set_dictionary(path, kind, lines):
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type = ?", (kind,))
        db.executemany("INSERT INTO dictionary_entries (dictionary_type, line_number, raw_line) VALUES (?, ?, ?)",
                       [(kind, n, line) for n, line in enumerate(lines, 1)])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def live(path, clock=None, log=None):
    empty = Callsigns.from_lines([])
    result = LiveDictionaries(path, Callsigns.from_lines(["віра: 68"]), empty, empty, "віра", log,
                              clock=clock or Clock())
    result.refresh(force=True)
    return result


# ---------- Словники ----------

def test_consumers_see_new_dictionary_without_restart(db_path):
    clock = Clock()
    dictionaries = live(db_path, clock)
    held = dictionaries.callsigns  # так його тримає EventProcessor
    prompts = []
    dictionaries.on_prompt(prompts.append)
    assert held.find("береза 300") == []

    set_dictionary(db_path, "callsigns", ["віра: 68", "береза: 300-310"])
    set_dictionary(db_path, "commands", ["відхід"])
    clock.now = 1.0
    assert dictionaries.refresh() is False, "не частіше ніж раз на 2 с"
    clock.now = 2.5
    assert dictionaries.refresh() is True
    assert held.find("береза 305") == [("береза", "305", 100)]
    assert "відхід" in dictionaries.commands.names
    assert prompts[-1] == "береза, відхід, віра"
    clock.now = 5.0
    assert dictionaries.refresh() is False, "без змін нічого не перебудовується"


def test_empty_callsigns_or_broken_database_keep_previous_dictionary(db_path):
    log = Mock()
    clock = Clock()
    dictionaries = live(db_path, clock, log)
    set_dictionary(db_path, "callsigns", [])
    clock.now = 3.0
    assert dictionaries.refresh() is False
    assert dictionaries.callsigns.find("віра 68") == [("віра", "68", 100)]
    assert log.error.called
    with patch("services.dictionary_loader.get_db", side_effect=sqlite3.OperationalError("locked")):
        assert dictionaries.refresh(force=True) is False
    assert dictionaries.callsigns.names == ["віра"]


def test_alias_change_rebuilds_dictionaries(db_path):
    clock = Clock()
    set_dictionary(db_path, "callsigns", ["наган: 110"])
    dictionaries = live(db_path, clock)
    revision = read_lists(db_path)["revision"]
    write_list(db_path, "aliases", [{"raw_line": "наган: нага", "enabled": True}], revision)
    text = text_lists.LiveTextLists(db_path)
    text.refresh(force=True)
    clock.now = 3.0
    assert dictionaries.refresh() is True, "алиаси змінились — словники перебудовано"
    assert dictionaries.callsigns.find("нага 110") == [("наган", "110", 100)]


# ---------- Налаштування обробки ----------

def test_runtime_settings_defaults_validation_and_conflicts(db_path):
    state = read_runtime_settings(db_path)
    assert state["revision"] == 0 and state["values"]["COOLDOWN_S"] == project_config.COOLDOWN_S
    assert state["values"]["GARBAGE_MIN_WORDS"] == 12
    for bad in ({"COOLDOWN_S": -1}, {"GARBAGE_MIN_WORDS": 2.5}, {"LOG_CALLSIGNS_ONLY": 1}, {"MIN_RMS": 0.1}):
        with pytest.raises(ValueError):
            write_runtime_settings(db_path, bad, 0)
    saved = write_runtime_settings(db_path, {"COOLDOWN_S": 10}, 0)
    assert saved["revision"] == 1 and saved["values"]["COOLDOWN_S"] == 10.0
    assert saved["values"]["GARBAGE_MIN_WORDS"] == 12, "інші значення не змінюються"
    with pytest.raises(RuntimeSettingsConflict):
        write_runtime_settings(db_path, {"COOLDOWN_S": 5}, 0)


def test_live_settings_apply_once_per_revision_and_survive_db_errors(db_path):
    applied, clock = [], Clock()
    settings = LiveRuntimeSettings(db_path, applied.append, Mock(), clock=clock)
    settings.refresh(force=True)
    write_runtime_settings(db_path, {"GARBAGE_MIN_WORDS": 5}, 0)
    clock.now = 1.0
    settings.refresh()
    assert len(applied) == 1
    clock.now = 2.5
    settings.refresh()
    clock.now = 5.0
    settings.refresh()
    assert [a["GARBAGE_MIN_WORDS"] for a in applied] == [12, 5]
    with patch("services.runtime_settings.get_db", side_effect=sqlite3.OperationalError("locked")):
        assert settings.refresh(force=True) is False
    assert settings.values["GARBAGE_MIN_WORDS"] == 5


def test_garbage_thresholds_are_parameters():
    text = "так так так так так"
    assert looks_like_garbage(text, 1.0) is False, "типово коротші за 12 слів не перевіряються"
    assert looks_like_garbage(text, 1.0, min_words=4) is True


# ---------- Повний шлях через фабрику ----------

def loud():
    return np.full(16000, 10000, dtype=np.int16).tobytes()


def test_receiver_applies_dictionary_and_settings_without_restart(db_path, tmp_path):
    from core.transcriber_factory import build_transcriber
    values = {k: getattr(project_config, k) for k in dir(project_config) if k.isupper()}
    config = SimpleNamespace(**{**values, "DB_PATH": str(db_path), "USE_LLM": False,
                                "SPECTRAL_NOTIFICATION_MODE": "off",
                                "NOTIFICATION_SOUNDS_DIR": tmp_path / "none"})
    empty = Callsigns.from_lines([])
    cooldown = Cooldown(3.0)
    with patch("core.transcriber_factory.WhisperRecognizer") as whisper:
        recognizer = whisper.return_value
        worker = build_transcriber(config=config, segments=Mock(), callsigns=Callsigns.from_lines(["віра: 68"]),
                                   commands=empty, watch=empty, prompt="віра", telegram_service=Mock(),
                                   cooldown=cooldown, log=Mock())
    worker.live_dictionaries.interval = 0
    worker.live_settings.interval = 0
    recognizer.extract_text.return_value = "Береза 300, прийом"

    def events():
        with sqlite3.connect(db_path) as db:
            return db.execute("SELECT callsigns FROM radio_events ORDER BY id").fetchall()

    worker.process_segment(loud())
    assert events() == [], "невідомий позивний — подію не записано"

    set_dictionary(db_path, "callsigns", ["віра: 68", "береза: 300"])
    write_runtime_settings(db_path, {"COOLDOWN_S": 30}, 0)
    worker.process_segment(loud())
    assert events() == [("береза 300",)]
    assert "береза" in recognizer.prompt, "підказку Whisper оновлено"
    assert cooldown.cooldown_s == 30.0

    from core.diagnostics import runtime
    recognizer.extract_text.return_value = "Віра 68 Віра 68 Віра 68"
    garbage = lambda: runtime.snapshot()["counters"].get("asr.garbage_filtered", 0)
    before = garbage()
    write_runtime_settings(db_path, {"COOLDOWN_S": 0}, 1)
    worker.process_segment(loud())
    assert garbage() == before and len(events()) == 2, "типово 6 слів — не сміття"
    write_runtime_settings(db_path, {"GARBAGE_MIN_WORDS": 4, "GARBAGE_MIN_UNIQUE_RATIO": 0.5}, 2)
    worker.process_segment(loud())
    assert garbage() == before + 1 and len(events()) == 2, "нові пороги діють без перезапуску"


# ---------- Локальна панель ----------

def test_local_panel_edits_runtime_settings(db_path, tmp_path):
    from services.user_store import init_users_db, bootstrap_admin
    from api.local_dashboard import create_dashboard
    init_users_db(db_path)
    bootstrap_admin(db_path, 1001)
    app = create_dashboard(db_path, KEY, samples_dir=tmp_path / "s", diagnostics_path=tmp_path / "d.json",
                           recordings_dir=tmp_path / "r")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 1)) as client:
        client.post("/session", json={"token": KEY}, headers={"Origin": ORIGIN})
        state = client.get("/api/runtime-settings").json()
        assert state["limits"]["GARBAGE_MIN_WORDS"] == {"min": 4, "max": 200, "type": "int"}
        body = {"values": {"COOLDOWN_S": 7.5}, "expected_revision": state["revision"]}
        assert client.put("/api/runtime-settings", json=body).status_code == 403
        assert client.put("/api/runtime-settings", json=body, headers={"Origin": ORIGIN}).json()["revision"] == 1
        assert client.put("/api/runtime-settings", json=body, headers={"Origin": ORIGIN}).status_code == 409
        bad = {"values": {"COOLDOWN_S": 500}, "expected_revision": 1}
        response = client.put("/api/runtime-settings", json=bad, headers={"Origin": ORIGIN})
        assert response.status_code == 422 and "від 0 до 120" in response.json()["detail"]


def test_receiver_applies_whisper_prompt_list_without_restart(db_path, tmp_path):
    from core.transcriber_factory import build_transcriber
    from services.text_lists import read_lists, write_list
    values = {k: getattr(project_config, k) for k in dir(project_config) if k.isupper()}
    config = SimpleNamespace(**{**values, "DB_PATH": str(db_path), "USE_LLM": False,
                                "SPECTRAL_NOTIFICATION_MODE": "off",
                                "NOTIFICATION_SOUNDS_DIR": tmp_path / "none"})
    empty = Callsigns.from_lines([])
    with patch("core.transcriber_factory.WhisperRecognizer") as whisper:
        worker = build_transcriber(config=config, segments=Mock(), callsigns=Callsigns.from_lines(["віра: 68"]),
                                   commands=empty, watch=empty, prompt="віра", telegram_service=Mock(),
                                   cooldown=Cooldown(3.0), log=Mock())
        assert whisper.call_args.kwargs["prompt"] == worker.live_dictionaries.prompt
    recognizer = whisper.return_value
    recognizer.prompt = whisper.call_args.kwargs["prompt"]
    worker.text_lists.interval = 0
    recognizer.extract_text.return_value = ""

    write_list(db_path, "whisper_prompt", [{"raw_line": "Віра 68 68", "enabled": True}],
               read_lists(db_path)["revision"])
    worker.process_segment(loud())
    assert recognizer.prompt == f"Віра 68 68. {worker.live_dictionaries.prompt}"

    write_list(db_path, "whisper_prompt", [{"raw_line": "Віра 68 68", "enabled": False}],
               read_lists(db_path)["revision"])
    worker.process_segment(loud())
    assert recognizer.prompt == worker.live_dictionaries.prompt
