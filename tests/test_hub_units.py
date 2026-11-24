"""Служби Pi: Mini App відкривається лише явно, замок бази й порти не змінюються."""
import re
from pathlib import Path

import radio345

HUB = Path(__file__).resolve().parent.parent / "deploy" / "hub"


def test_every_unit_runs_profile_process():
    commands = {c for c in ("-m " + v[1] for v in radio345.COMMANDS.values())}
    units = {p.stem: p.read_text(encoding="utf-8") for p in HUB.glob("*.service")}
    assert set(units) == set(radio345.UNITS)
    for text in units.values():
        assert re.search(r"^ExecStart=@ROOT@/\.venv/bin/python (-m \S+)$", text, re.M).group(1) in commands
        assert "NoNewPrivileges=yes" in text and "User=@USER@" in text


def test_public_access_is_explicit():
    profile = radio345.load_profile("pi")
    assert set(profile["manual"]) == {"api", "tunnel"}
    assert radio345.selected(profile) == [n for n in radio345.COMMANDS if n in ("node", "asr", "panel")]
    script = (HUB / "install.sh").read_text(encoding="utf-8")
    default = re.search(r'"" \) ?|""\) units="([^"]+)"', script).group(1)
    assert "api" not in default and "tunnel" not in default


def test_import_stops_everything_that_opens_database():
    script = (HUB / "import.sh").read_text(encoding="utf-8")
    before = script.split('mv "$root')[0].split("for f in")[0]
    for unit in ("radio345-api", "radio345-panel", "radio345-asr", "radio345-node"):
        assert re.search(rf"systemctl stop [^\n]*{unit}", before)
