"""Панель у режимі Tailscale: лише дозволені імена й пристрої власника."""
import pytest
from fastapi.testclient import TestClient

from server.api import local_dashboard
from shared.event_store import init_db

HOSTS = ["100.64.0.9", "pi.example.ts.net", "pi"]


def client(tmp_path, peer, base):
    db = tmp_path / "events.db"
    init_db(db)
    app = local_dashboard.create_dashboard(db, "k" * 40, recordings_dir=tmp_path / "r", samples_dir=tmp_path / "s",
                                           hosts=HOSTS, client_ok=lambda ip: ip == "100.64.0.1")
    return TestClient(app, base_url=base, client=(peer, 1))


@pytest.mark.parametrize("name", HOSTS)
def test_owner_device_logs_in_by_any_tailnet_name(tmp_path, name):
    base = f"http://{name}:8766"
    c = client(tmp_path, "100.64.0.1", base)
    assert c.get("/").status_code == 200
    assert c.get("/api/summary").status_code == 401
    assert c.post("/session", json={"token": "k" * 40}, headers={"origin": base}).status_code == 204


@pytest.mark.parametrize("peer", ["100.64.0.2", "192.168.1.5", "127.0.0.1"])
def test_other_devices_are_rejected(tmp_path, peer):
    assert client(tmp_path, peer, "http://pi:8766").get("/").status_code == 403


def test_foreign_host_and_origin_are_rejected(tmp_path):
    assert client(tmp_path, "100.64.0.1", "http://evil.example:8766").get("/").status_code == 403
    c = client(tmp_path, "100.64.0.1", "http://pi:8766")
    assert c.post("/session", json={"token": "k" * 40}, headers={"origin": "http://evil.example"}).status_code == 403


def test_default_panel_stays_local(tmp_path):
    init_db(tmp_path / "events.db")
    app = local_dashboard.create_dashboard(tmp_path / "events.db", "k" * 40, recordings_dir=tmp_path / "r",
                                           samples_dir=tmp_path / "s")
    assert TestClient(app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 1)).get("/").status_code == 200
    assert TestClient(app, base_url="http://127.0.0.1:8766", client=("100.64.0.1", 1)).get("/").status_code == 403


def test_tailnet_mode_requires_tailscale(monkeypatch):
    monkeypatch.setattr("shared.radio_stream.tailscale_ipv4", lambda: None)
    with pytest.raises(SystemExit):
        local_dashboard.tailnet_access()
