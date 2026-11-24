"""Конфлікт пристроїв: перевіряється сам, повторних спроб немає, господар не поступається новому."""
import logging
import sys
from types import SimpleNamespace

import pytest

import radio345
from server import asr_service
from shared import conflict as c


def polls(*codes):
    items = iter(codes)
    return lambda: next(items)


@pytest.mark.parametrize("codes,busy", [((200, 200, 200, 200), False), ((409, 409), True), ((200, 409, 200, 409), True),
                                        ((409, 200, 200, 200), False), ((500, 502, 200, 200), False)])
def test_preflight(codes, busy):
    assert c.bot_busy(polls(*codes)) is busy


def test_preflight_without_network_does_not_block_start():
    def down():
        raise OSError
    assert c.bot_busy(down) is False


def owner():
    now = [1000.0]
    return c.BotOwner(lambda: now[0]), now


def test_newcomer_yields_after_two_conflicts():
    o, _ = owner()
    assert o.conflict() is False and o.conflict() is True


def test_established_owner_never_yields():
    o, now = owner()
    for _ in range(6):
        o.ok()
        now[0] += 30
    assert not any(o.conflict() for _ in range(20))


def test_device_that_slept_yields_when_it_wakes():
    o, now = owner()
    for _ in range(6):
        o.ok()
        now[0] += 30
    now[0] += c.GAP_S + 1   # сон Mac: бот тим часом працює на Pi
    assert o.conflict() is False and o.conflict() is True


def test_conflicts_are_not_accumulated_between_successes():
    o, _ = owner()
    assert o.conflict() is False
    o.ok()
    assert o.conflict() is False


def test_listener_stops_on_confirmed_conflict(monkeypatch):
    from server import telegram_updates as u
    monkeypatch.setattr(u, "load_telegram_offset", lambda db: None)
    monkeypatch.setattr(u.time, "sleep", lambda s: None)
    calls = []

    class Service(u.UpdatesMixin):
        _updates_offset, db_path, log = None, "x", logging.getLogger("t")

        def _request(self, *a, **k):
            calls.append(1)
            return SimpleNamespace(status_code=409)
    service = Service()
    service.callback_listener()
    assert service.bot_conflict is True and len(calls) == c.BUSY_HITS


def test_service_exits_after_conflict():
    service = SimpleNamespace(bot_conflict=True, start_maintenance=lambda: pytest.fail("служба має зупинитися"))
    with pytest.raises(asr_service.BotConflict):
        asr_service.Bot(service, logging.getLogger("t")).tick()


def test_stop_uses_the_code_without_restart():
    with pytest.raises(SystemExit) as done:
        c.stop("x")
    assert done.value.code == c.EXIT


def test_r345_does_not_restart_blocked_service_until_asked(monkeypatch):
    lines, started = [], []
    child = radio345.Child("asr", ("-c", ""), {}, lambda n, line: lines.append(line))
    monkeypatch.setattr(radio345.home_marker, "foreign", lambda root: None)   # не позначка цього Mac
    child.process = SimpleNamespace(poll=lambda: c.EXIT, pid=10 ** 9)
    monkeypatch.setattr(child, "_start", lambda now: started.append(now))
    monkeypatch.setattr(child, "_reap", lambda: None)
    for _ in range(5):
        child.tick()
    assert started == [] and child.state()["blocked"] is True and any("зайнято" in line for line in lines)
    child.restart()
    child.tick()
    assert len(started) == 1 and "blocked" not in child.state()


def test_ordinary_crash_is_still_restarted(monkeypatch):
    started = []
    child = radio345.Child("asr", ("-c", ""), {}, lambda *a: None, clock=lambda: 1e9)
    child.process = SimpleNamespace(poll=lambda: 1, pid=10 ** 9)
    monkeypatch.setattr(child, "_start", lambda now: started.append(now))
    monkeypatch.setattr(child, "_reap", lambda: None)
    child.tick()
    assert not child.blocked and child.next_start > 1e9


def test_units_do_not_restart_after_conflict():
    from pathlib import Path
    hub = Path(radio345.ROOT) / "deploy" / "hub"
    for unit in ("asr", "tunnel"):
        assert f"RestartPreventExitStatus={c.EXIT}" in (hub / f"radio345-{unit}.service").read_text(encoding="utf-8")


def test_tunnel_reports_busy_domain(tmp_path):
    import stat
    import threading
    from server import miniapp_tunnel as t
    fake = tmp_path / "ngrok"
    fake.write_text(f"#!{sys.executable}\nprint('lvl=eror err=ERR_NGROK_334', flush=True)\nraise SystemExit(1)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    lines = []
    code = t.run("https://x.dev", str(fake), threading.Event(), lines.append, reachable=lambda: True, urls=lambda: [],
                 page=lambda: "https://x.dev")
    assert code == c.EXIT and any("іншому пристрої" in line for line in lines)
