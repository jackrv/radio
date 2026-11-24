"""Історія доступна лише приватній локальній панелі."""

from pathlib import Path
from html.parser import HTMLParser
import re

from shared.diagnostics import RuntimeDiagnostics
from tests.test_local_dashboard import local
from tests.test_miniapp_management import setup
from tests.test_web_api import auth_header
from shared.paths import place


def test_local_panel_lists_and_reads_previous_sessions(local):
    client, _, tmp = local
    first = RuntimeDiagnostics()
    first.configure(tmp / "diagnostics.json")
    first.count("asr.measured_segments", 27)
    first.finish()
    second = RuntimeDiagnostics()
    second.configure(tmp / "diagnostics.json")
    listing = client.get("/api/diagnostics/history")
    assert listing.status_code == 200
    assert {row["session"] for row in listing.json()["items"]} == {first.session, second.session}
    assert listing.json()["retention_days"] == 14
    previous = client.get("/api/diagnostics/history/" + first.session)
    assert previous.status_code == 200
    assert previous.json()["counters"]["asr.measured_segments"] == 27
    assert previous.json()["session_status"] == "finished"
    assert previous.json()["session"] == first.session
    assert previous.headers["cache-control"] == "no-store"
    assert client.get("/api/diagnostics").json()["session"] == second.session


def test_local_history_requires_session_auth_and_rejects_invalid_ids(local):
    client, _, _ = local
    assert client.get("/api/diagnostics/history/invalid.json").status_code == 422
    assert client.get("/api/diagnostics/history/" + "a" * 32).status_code == 404
    client.cookies.clear()
    assert client.get("/api/diagnostics/history").status_code == 401
    assert client.get("/api/diagnostics/history/" + "a" * 32).status_code == 401


def test_history_read_does_not_create_directory(local):
    client, _, tmp = local
    assert client.get("/api/diagnostics/history").json()["items"] == []
    assert not (place(tmp, ".local-diagnostics")).exists()


def test_history_is_not_exposed_on_telegram_api(setup):
    client, _ = setup
    for route in ["/api/diagnostics/history", "/api/diagnostics/history/" + "a" * 32]:
        assert client.get(route, headers=auth_header(1001)).status_code == 404


def test_local_history_controls_exist_and_ids_are_unique():
    root = Path(__file__).resolve().parents[1]

    class Ids(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])

    parser = Ids()
    parser.feed((root / "local_admin/index.html").read_text())
    assert len(parser.ids) == len(set(parser.ids))
    from server.api.local_dashboard import panel_script
    script = panel_script()
    for name in re.findall(r'\$\("(history-[a-z-]+)"\)', script):
        assert name in parser.ids
    assert 'await diagnosticHistory();' in script
