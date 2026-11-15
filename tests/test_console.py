import io
import logging
import time

import pytest

from core.console import ConsoleDashboard
from core.log_security import RedactingFormatter


def test_console_filters_noise_and_keeps_traceback_in_file_only():
    stream = io.StringIO()
    console = ConsoleDashboard(stream=stream)
    console.setFormatter(RedactingFormatter('%(message)s', None, 'SECRET'))
    console.handle(logging.makeLogRecord({'msg': 'segment detail', 'levelno': logging.INFO}))
    try:
        raise ValueError('SECRET traceback detail')
    except ValueError:
        import sys
        console.handle(logging.makeLogRecord({'msg': 'Telegram SECRET unavailable', 'levelno': logging.ERROR, 'exc_info': sys.exc_info()}))
    text = stream.getvalue()
    assert 'segment detail' not in text
    assert 'SECRET' not in text
    assert '[REDACTED]' in text
    assert 'Traceback' not in text
    assert '\x1b' not in text


def test_statistics_and_old_session():
    console = ConsoleDashboard(stream=io.StringIO())
    data = {'started_at': time.time(), 'updated_at': time.time(),
            'gauges': {'receiver.state': 'paused', 'audio.segments_dropped': 3},
            'counters': {'asr.outcome.ready': 4, 'asr.outcome.ready_fallback': 2}}
    text = '\n'.join(console.lines(data))
    assert 'Пауза' in text
    assert 'Розпізнано: 6' in text
    assert '0 кадрів · 3 сегм.' in text
    data['session_status'] = 'finished'
    assert 'Зупинено' in console.lines(data)[0]


def test_tty_keeps_bounded_events_and_refreshes_header():
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream)
    for n in range(20):
        console.add(f'Event {n}')
    assert len(console.events) == 8
    frame = stream.getvalue()
    assert 'Radio 345' in frame
    assert '\x1b[1;30;43m' in frame, 'жовта плашка запуску'
    assert 'Event 19' in frame
    assert 'Event 0\n' not in frame
    console.close()


def test_alternate_screen_is_restored_once():
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream).start()
    console.close()
    console.close()
    text = stream.getvalue()
    assert text.count("\x1b[?1049h") == 1
    assert text.count("\x1b[?1049l") == 1
    assert "\x1b[?25h" in text


def test_child_progress_is_not_a_warning():
    from core.console import child_event
    for line in ("\n", "Fetching 9 files: 0%| | 0/9", "Fetching 4 files: 100%|████| 4/4", "Traceback (most recent call last):"):
        assert child_event("radio", line) is None
    assert child_event("api", "ERROR: service unavailable") == ("api: service unavailable", logging.ERROR)


def test_managed_events_keep_severity_and_one_message(monkeypatch):
    from core.console import child_event
    monkeypatch.setenv("RADIO345_MANAGED_CONSOLE", "1")
    stream = io.StringIO()
    console = ConsoleDashboard(stream=stream)
    console.setFormatter(RedactingFormatter("%(message)s", None, "SECRET"))
    console.handle(logging.makeLogRecord({"msg": "Telegram SECRET unavailable", "levelno": logging.ERROR}))
    assert child_event("radio", stream.getvalue()) == ("Telegram [REDACTED] unavailable", logging.ERROR)
    assert "[WARNING]" not in stream.getvalue()


def test_mouse_and_keyboard_are_not_captured(monkeypatch):
    import sys
    from unittest.mock import Mock
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    input_stream = Mock()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", stream)
    console = ConsoleDashboard(stream=stream).start()
    console.close()
    assert not input_stream.mock_calls
    text = stream.getvalue()
    assert "?1000h" not in text
    assert "?1006h" not in text



def test_render_never_scrolls_even_in_a_tiny_terminal(monkeypatch):
    import os
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream)
    for width, height in ((80, 24), (12, 3), (1, 1), (120, 10)):
        monkeypatch.setattr(console, "terminal_size", lambda: os.terminal_size((width, height)))
        stream.seek(0)
        stream.truncate()
        console.add("⚠️ 模型 🙂 " * 30, logging.WARNING)
        text = stream.getvalue()
        assert "\n" not in text and "\r" not in text
        assert f"\x1b[{height};1H" in text
        assert f"\x1b[{height + 1};1H" not in text
    assert console.fit("🙂🙂abc", 5) == "🙂🙂a"


def test_size_uses_real_tty_over_environment(monkeypatch):
    import os
    from unittest.mock import Mock
    stream = Mock()
    stream.fileno.return_value = 77
    console = ConsoleDashboard(stream=stream)
    monkeypatch.setenv("LINES", "100")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((42, 7)) if fd == 77 else None)
    assert console.terminal_size() == (42, 7)


def test_admin_link_stays_pinned_when_event_history_overflows(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream)
    url = "http://127.0.0.1:8766"
    console.set_admin_url(url)
    initial = stream.getvalue()
    assert f"\x1b]8;;{url}\x1b\\" in initial
    assert "Адмін-панель:" in initial and url in initial
    link_row = next(row for row in initial.split("\x1b[")[1:] if url in row and row[0].isdigit())
    link_position = "\x1b[" + link_row.split("H", 1)[0] + "H"
    stream.seek(0)
    stream.truncate()
    for n in range(20):
        console.add(f"Event {n}")
    # A stable link row is neither erased nor rewritten, so selection survives.
    assert url not in stream.getvalue()
    assert link_position not in stream.getvalue()
    assert all(url not in event for event in console.events)
    assert console.admin_url == url
    console.close()


def test_unchanged_rows_are_not_repainted():
    stream = io.StringIO()
    console = ConsoleDashboard(stream=stream)
    console.render()
    stream.seek(0)
    stream.truncate()
    console.render()
    assert stream.getvalue() == ""


def test_ngrok_timeout_dns_auth_are_user_events_and_ignore_background_updates():
    from core.console import child_event
    event = child_event('ngrok', 'lvl=eror msg="failed to reconnect session" err="dial tcp connect.ngrok-agent.com:443: i/o timeout"')
    assert event[1] == logging.ERROR
    assert 'час підключення' in event[0] and 'VPN' in event[0]
    assert 'DNS' in child_event('ngrok', 'lvl=eror err="no such host"')[0]
    assert 'авторизації' in child_event('ngrok', 'lvl=eror err="authentication failed ERR_NGROK_4018"')[0]
    assert child_event('ngrok', 'lvl=warn msg="failed to check for update"') is None
    assert child_event('ngrok', 'lvl=info msg=start pg=/api/tunnels') is None


def test_failure_is_pinned_despite_cleanup_event_overflow():
    console = ConsoleDashboard(stream=io.StringIO())
    console.set_problem('ngrok: час підключення вичерпано')
    for n in range(20):
        console.add(f'Stopping service {n}')
    lines = console.lines({})
    assert 'зупинено' in lines[0].lower()
    assert 'ngrok' in lines[1]


def test_apple_terminal_uses_plain_url_and_native_open_hint(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream)
    console.set_admin_url("http://127.0.0.1:8766")
    text = stream.getvalue()
    assert "http://127.0.0.1:8766" in text
    assert "⌘ + подвійний клік" in text
    assert "\x1b]8;;" not in text
    stream.seek(0)
    stream.truncate()
    console.add("New event")
    assert "http://127.0.0.1:8766" not in stream.getvalue()
    assert "?1000h" not in text
    console.close()


def _screen(console, columns, height, data):
    import os
    import re
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    console.stream = Terminal()
    console.tty = True
    console.provider = lambda: data
    console.terminal_size = lambda: os.terminal_size((columns, height))
    console._painted_rows.clear()
    console.render()
    text = console.stream.getvalue()
    rows = {int(m[1]): re.sub(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\", "", m[2])
            for m in re.finditer(r"\x1b\[(\d+);1H(.*?)\x1b\[K", text, re.S)}
    return rows, text


def _live(**gauges):
    now = time.time()
    return {"started_at": now - 3725, "updated_at": now, "session_status": "running",
            "gauges": {"receiver.state": "listening", "models.whisper": "ready", "models.qwen": "loading", **gauges},
            "counters": {"asr.outcome.ready": 12},
            "asr_latency": {"ready": {"total_latency_s": {"mean": 2.4, "recent_p95": 4.9}},
                            "recent_segments": [{"text_ready_unix_s": now - 30}]}}


@pytest.mark.parametrize("columns", [80, 54, 30])
def test_layout_fits_window_and_pins_footer(columns):
    console = ConsoleDashboard(stream=io.StringIO())
    console.started = time.time() - 4000
    console.admin_url = "http://127.0.0.1:8766"
    for n in range(12):
        console.add(f"Подія номер {n} з досить довгим поясненням, яке не вміщується у вузьке вікно", logging.WARNING)
    rows, _ = _screen(console, columns, 20, _live())
    assert all(console.width(text) <= columns - 1 for text in rows.values())
    assert "Ctrl+C" in rows[20] or "radio.log" in rows[20], "підвал у нижньому рядку"
    assert "робота 01:02:05" in rows[1] or columns < 50
    text = "\n".join(rows.values())
    assert "Розпізнано" in text and "12" in text
    assert "завантаження" in text or columns < 60


def test_values_use_colors_by_meaning():
    console = ConsoleDashboard(stream=io.StringIO())
    console.started = time.time() - 4000
    _, text = _screen(console, 80, 24, _live(**{"audio.segments_dropped": 2}))
    assert "\x1b[31m0 кадрів · 2 сегм." in text, "втрати — червоним"
    assert "\x1b[1;30;42m ● ПРИЙМАННЯ " in text
    assert "\x1b[33mзавантаження" in text


def test_long_failure_is_wrapped_not_cut():
    console = ConsoleDashboard(stream=io.StringIO())
    console.problem = "ngrok: перевищено час підключення до сервера. Перевірте інтернет або VPN і повторіть запуск."
    rows, _ = _screen(console, 60, 16, {})
    text = " ".join(rows.values())
    assert "повторіть запуск." in text and "✖ ЗУПИНЕНО" in rows[1]



def test_heartbeat_keeps_receiving_during_30_second_statistics_interval(monkeypatch):
    now = time.time()
    console = ConsoleDashboard(stream=io.StringIO(), receiver_provider=lambda: None)
    console.started = now - 100
    console.receiver_requested_at = now - 90
    data = _live()
    for age in (0, 14, 16, 29, 45, 90):
        monkeypatch.setattr(time, "time", lambda: now + age)
        data["_receiver"] = {"state":"listening", "updated_at":now + age}
        assert "ПРИЙМАННЯ" in console._status(data)[0]
    assert not console._fresh(data)
    assert "статистика" in " ".join(console.lines(data))


def test_state_transitions_and_stale_heartbeat(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    console = ConsoleDashboard(stream=io.StringIO(), receiver_provider=lambda: None)
    console.started = now - 100
    console.receiver_requested_at = None
    old = {"_receiver":{"state":"listening","updated_at":now}}
    assert "ЗАПУСК" in console._status(old)[0]  # Receiver not requested yet.
    console.expect_receiver()
    assert "ЗАПУСК" in console._status({"_receiver":{"state":"listening","updated_at":now-1}})[0]
    for state, label in (("listening","ПРИЙМАННЯ"),("paused","ПАУЗА"),("stopped","ЗУПИНЕНО")):
        assert label in console._status({"_receiver":{"state":state,"updated_at":now}})[0]
    monkeypatch.setattr(time,"time",lambda:now+11)
    assert "НЕМАЄ ЗВ’ЯЗКУ" in console._status(old)[0]
    monkeypatch.setattr(time,"time",lambda:now+61)
    assert "НЕМАЄ ЗВ’ЯЗКУ" in console._status({})[0]


def test_diagnostic_failures_are_distinct_from_startup(monkeypatch):
    console=ConsoleDashboard(stream=io.StringIO())
    def missing(): raise FileNotFoundError()
    def invalid(): raise ValueError('invalid JSON')
    def denied(): raise PermissionError()
    for provider, expected in ((missing,'ЗАПУСК'),(invalid,'ПОМИЛКА ДІАГНОСТИКИ'),(denied,'ПОМИЛКА ДІАГНОСТИКИ')):
        console.provider=provider
        assert expected in console._status(console._read_snapshot())[0]
    console.receiver_provider=lambda: (_ for _ in ()).throw(OSError('busy'))
    assert 'ПОМИЛКА ДІАГНОСТИКИ' in console._status(console._read_snapshot())[0]


def test_statistics_remain_fresh_at_29_seconds_without_heartbeat(monkeypatch):
    now=time.time()
    console=ConsoleDashboard(stream=io.StringIO())
    console.started=now-100
    data=_live()
    monkeypatch.setattr(time,'time',lambda:now+29)
    assert console._fresh(data)
    assert 'ПРИЙМАННЯ' in console._status(data)[0]
