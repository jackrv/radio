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
    frame = stream.getvalue().rsplit('\x1b[H\x1b[J', 1)[1]
    assert frame.startswith('Radio 345')
    assert 'Event 19' in frame
    assert 'Event 0\n' not in frame
    console.close()
