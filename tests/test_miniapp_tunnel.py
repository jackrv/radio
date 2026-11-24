"""Тунель Mini App: лише постійний домен, без змін у репозиторії, публічно — тільки явно."""
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

import radio345
from services import miniapp_tunnel as t
from tools import profiles

URL = "https://radio.example-tunnel.dev"


def fake_ngrok(tmp_path, body):
    path = tmp_path / "ngrok"
    path.write_text(f"#!{sys.executable}\nimport sys,time,signal\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def start(executable, stop, lines, **kwargs):
    result = []
    options = {"reachable": lambda: True, "urls": lambda: [URL], "page": lambda: URL, **kwargs}
    thread = threading.Thread(target=lambda: result.append(t.run(URL, executable, stop, lines.append, **options)), daemon=True)
    thread.start()
    return thread, result


def until(check, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not check():
        time.sleep(0.02)
    return check()


def test_opens_fixed_domain_and_stops_ngrok(tmp_path):
    marker = tmp_path / "args"
    ngrok = fake_ngrok(tmp_path, f"open({str(marker)!r},'w').write(' '.join(sys.argv[1:]))\ntime.sleep(60)")
    stop, lines = threading.Event(), []
    thread, result = start(ngrok, stop, lines)
    assert until(lambda: any(URL in line for line in lines)) and until(marker.exists)
    stop.set()
    thread.join(15)
    assert result == [0] and f"--url {URL}" in marker.read_text()


def test_waits_for_api_before_opening_anything(tmp_path):
    marker = tmp_path / "started"
    ngrok = fake_ngrok(tmp_path, f"open({str(marker)!r},'w')\ntime.sleep(60)")
    stop, lines = threading.Event(), []
    thread, result = start(ngrok, stop, lines, reachable=lambda: False)
    time.sleep(0.4)
    stop.set()
    thread.join(5)
    assert result == [0] and not marker.exists() and lines == ["Чекаю API на 127.0.0.1:8765"]


def test_other_public_address_is_closed(tmp_path):
    thread, result = start(fake_ngrok(tmp_path, "time.sleep(60)"), threading.Event(), [],
                           urls=lambda: ["https://random.example.dev"])
    thread.join(15)
    assert result == [1]


def test_ngrok_exit_and_silence_are_failures(tmp_path):
    thread, result = start(fake_ngrok(tmp_path, "sys.exit(7)"), threading.Event(), [], urls=lambda: [])
    thread.join(10)
    assert result == [1]
    thread, result = start(fake_ngrok(tmp_path, "time.sleep(60)"), threading.Event(), [], urls=lambda: [], start_s=0.2)
    thread.join(15)
    assert result == [1]


def test_mismatch_only_warns_and_never_edits(tmp_path):
    (tmp_path / "apps/miniapp").mkdir(parents=True)
    page = tmp_path / "apps/miniapp/index.html"
    page.write_text('<meta name="radio345-api" content="https://old.example.dev">')
    before = page.read_bytes()
    stop, lines = threading.Event(), []
    thread, _ = start(fake_ngrok(tmp_path, "time.sleep(60)"), stop, lines, page=lambda: t.page_api_url(tmp_path))
    assert until(lambda: any("інша" in line for line in lines))
    stop.set()
    thread.join(15)
    assert page.read_bytes() == before


def test_visitor_addresses_do_not_reach_the_log():
    assert not t.important('t=1 lvl=info msg="join connections" r=203.0.113.5:1')
    assert t.important('t=1 lvl=eror msg="session closed"')


@pytest.mark.parametrize("url", ["", "http://a.dev", "https://a", "https://a.dev:1", "https://u:p@a.dev", "https://a.dev/x",
                                 "https://a.dev?q=1", "https://a_b.dev", None])
def test_bad_addresses_are_rejected(url):
    with pytest.raises(ValueError):
        t.valid_https_endpoint(url)


def test_module_never_touches_git():
    source = Path(t.__file__).read_text(encoding="utf-8")
    assert '"git"' not in source and "write_text" not in source


def test_plain_up_never_opens_the_tunnel():
    for name in profiles.names():
        assert "tunnel" not in radio345.selected(radio345.load_profile(name)), name
    assert radio345.selected(radio345.load_profile("mac-dev"), ["api", "tunnel"]) == ["api", "tunnel"]
