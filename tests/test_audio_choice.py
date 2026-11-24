"""Вибір звуку: файл машини, з перевіркою; Pi — через ssh з екранованими аргументами."""
import json
import shlex
from types import SimpleNamespace

import pytest

from server import devices as d
from shared import audio_choice as a
from shared.paths import place

VALUES = {"RADIO_INPUT_DEVICE": "USB", "RADIO_OUTPUT_DEVICE": ""}


@pytest.mark.parametrize("bad", ["{", "[]", '{"values": 1, "revision": 1}', '{"values": {"RADIO_INPUT_DEVICE": "a\\nb"}, "revision": 1}',
                                 '{"values": {}, "revision": "1"}'])
def test_damaged_file_means_defaults(tmp_path, bad):
    (place(tmp_path, a.NAME)).write_text(bad)
    target = SimpleNamespace()
    a.apply(tmp_path, target)
    assert vars(target) == {} and a.read(tmp_path)["revision"] == 0


def test_env_file_is_reported_as_override(tmp_path, monkeypatch):
    monkeypatch.delenv("RADIO_INPUT_DEVICE", raising=False)
    (tmp_path / ".env").write_text("RADIO_INPUT_DEVICE=USB\n")
    assert a.read(tmp_path)["env_override"] == ["RADIO_INPUT_DEVICE"]


def ssh(code=0, data=None):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=code, stdout=json.dumps(data or {}), stderr="")
    run.seen = seen
    return run


def test_pi_arguments_are_quoted():
    run = ssh(data={"settings": {}})
    payload = json.dumps({"values": {"RADIO_INPUT_DEVICE": "a'; rm -rf /"}, "revision": 0})
    d.audio_pi("pi@test", "write", payload, run=run)
    remote = run.seen[0][-1]
    assert shlex.split(remote)[-2:] == ["write", payload]


@pytest.mark.parametrize("code,text", [(255, "SSH"), (1, "оновіть")])
def test_pi_failures_are_explained(code, text):
    with pytest.raises(d.Refused, match=text):
        d.audio_pi("pi@test", "read", run=ssh(code))


def test_pi_save_reports_conflict_and_rejects_bad_names(monkeypatch, tmp_path):
    state = d.Devices("x", tmp_path, "pi@test")
    monkeypatch.setattr(d, "audio_pi", lambda host, *args: {"conflict": "змінилося"})
    with pytest.raises(a.Conflict):
        state.save_audio("pi", VALUES, 0)
    monkeypatch.setattr(d, "audio_pi", lambda *args: pytest.fail("звернення до Pi"))
    with pytest.raises(ValueError):
        state.save_audio("pi", {**VALUES, "RADIO_INPUT_DEVICE": "a\nb"}, 0)


def test_command_line_reads_and_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(a, "ROOT", tmp_path)
    assert a.main(["write", json.dumps({"values": VALUES, "revision": 0})]) == 0
    assert json.loads((place(tmp_path, a.NAME)).read_text())["revision"] == 1
    a.main(["write", json.dumps({"values": VALUES, "revision": 0})])
    assert "conflict" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert a.main(["x"]) == 2
