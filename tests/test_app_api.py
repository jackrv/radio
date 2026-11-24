"""Контракт застосунків: старий клієнт працює, невідома версія дістає зрозумілу відмову."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from shared import radio_stream as r


@pytest.fixture
def base():
    channel = SimpleNamespace(listeners=0)
    stream = SimpleNamespace(
        authorized=lambda ip: stream.allow, allow=True, transmit_enabled=False, session="s1", _tx_active=False, ptt_key=None, mode="watch",
        listeners=0, channels={"live": channel, "live_clean": channel, "selected": channel}, history=lambda after: [],
        peers=SimpleNamespace(is_home=lambda ip: True), gate=SimpleNamespace(dropped=0), log=None,
        mixer=SimpleNamespace(pending_s=0.0), clip_pcm=lambda i: None, on_air=lambda: False)
    stream.features = lambda: r.RadioStream.features(stream)
    server = ThreadingHTTPServer(("127.0.0.1", 0), r._handler_for(stream))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", stream=stream)
    server.shutdown()
    server.server_close()


def get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as response:
            return response.status, dict(response.headers), json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, dict(exc.headers), json.loads(body) if body.startswith(b"{") else body


def test_version_lists_contract_and_features(base):
    status, headers, data = get(base.url + "/version")
    assert status == 200 and data["api"] == r.API_VERSION == int(headers["X-Radio345-Api"])
    assert r.API_VERSION in data["supported"] and "transmit" not in data["features"]
    base.stream.transmit_enabled = True
    assert "transmit" in get(base.url + "/version")[2]["features"]


def test_old_client_without_version_keeps_working(base):
    assert get(base.url + "/events?after=0")[0] == 200


@pytest.mark.parametrize("query,headers", [("&api=1", {}), ("", {"X-Radio345-Api": "1"})])
def test_supported_version_is_accepted(base, query, headers):
    assert get(base.url + "/events?after=0" + query, headers)[0] == 200


@pytest.mark.parametrize("query,headers", [("&api=99", {}), ("&api=x", {}), ("", {"X-Radio345-Api": "99"})])
def test_unknown_version_gets_clear_refusal_but_version_stays_readable(base, query, headers):
    status, _, data = get(base.url + "/events?after=0" + query, headers)
    assert status == 426 and data["error"] == "unsupported_api" and data["supported"] == list(r.API_SUPPORTED)
    assert get(base.url + "/version?api=99")[0] == 200


def test_foreign_device_does_not_learn_the_version(base):
    base.stream.allow = False
    assert get(base.url + "/version")[0] == 403


def test_events_fields_used_by_released_app_are_stable(base):
    fields = {"clips", "listeners", "live_listeners", "at_home", "signals_dropped", "transmit", "transmitting", "ptt_key",
              "pending_s", "mode", "session"}
    assert fields <= set(get(base.url + "/events")[2]), "прибирання поля — несумісна зміна: потрібна нова версія"
