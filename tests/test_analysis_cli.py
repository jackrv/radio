import json
import sqlite3

import numpy as np

import config
from shared.recordings import RecordingStore
from tests.test_miniapp_management import setup  # noqa: F401  (фікстура тимчасової бази)
from lab import analysis


def test_cli_reads_the_same_analysis_as_the_panel(setup, tmp_path, monkeypatch, capsys):
    _, path = setup
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dictionary_entries WHERE dictionary_type='callsigns'")
        db.execute("INSERT INTO dictionary_entries (dictionary_type,line_number,raw_line) VALUES ('callsigns',1,'клен: 48')")
    store = RecordingStore(tmp_path / "recordings")
    pcm = np.full(16000, 2000, dtype=np.int16).tobytes()
    good = store.save(pcm, 16000, {"status": "ready", "raw_text": "клен 48 прийом", "final_text": "Клен 48, прийом",
                                   "whisper_segments": [{"avg_logprob": -0.2}], "signal": {"duration_s": 3, "rms_dbfs": -25}})
    bad = store.save(pcm, 16000, {"status": "ready", "raw_text": "клей 48 прийом", "final_text": "клей 48 прийом",
                                  "whisper_segments": [{"avg_logprob": -1.2}], "signal": {"duration_s": 3, "rms_dbfs": -25}})
    store.review(good, "Клен 48, прийом", "speech", 0)
    store.review(bad, "Клен 48, прийом", "speech", 0)
    monkeypatch.setattr(config, "RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(config, "DB_PATH", str(path))

    assert analysis.main(["errors", "--json"]) == 0
    errors = json.loads(capsys.readouterr().out)
    assert errors["with_edits"] == 1 and errors["categories"]["callsigns"]["ids"] == [bad]

    analysis.main(["records", "callsigns", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [bad] and rows[0]["reference"] == "Клен 48, прийом"

    analysis.main(["records", "edit_free"])
    assert good[:8] in capsys.readouterr().out

    analysis.main(["summary", "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["effort"]["no_edits"] == 1 and summary["queues"]["no_roles"] == 2
