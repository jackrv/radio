import io
import logging
import time

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
    assert '0 / 3' in text
    data['updated_at'] -= 60
    assert 'немає актуальних даних' in console.lines(data)[0]


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
    assert '\x1b[33m' in frame
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


def test_admin_link_stays_pinned_when_event_history_overflows():
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = ConsoleDashboard(stream=stream)
    url = "http://127.0.0.1:8766"
    console.set_admin_url(url)
    initial = stream.getvalue()
    assert f"\x1b]8;;{url}\x1b\\" in initial
    assert f"Адмін-панель: {url}" in initial
    stream.seek(0)
    stream.truncate()
    for n in range(20):
        console.add(f"Event {n}")
    # A stable link row is neither erased nor rewritten, so selection survives.
    assert url not in stream.getvalue()
    assert "\x1b[3;1H" not in stream.getvalue()
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
