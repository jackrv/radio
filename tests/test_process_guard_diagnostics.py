import io
import signal
from unittest.mock import Mock

from services import process_guard


def test_guard_logs_original_signal_before_shell_wraps_exit_code(monkeypatch, capsys):
    child = Mock(stdout=io.StringIO(''), returncode=-signal.SIGBUS)
    child.poll.return_value = -signal.SIGBUS
    child.wait.return_value = -signal.SIGBUS
    monkeypatch.setattr(process_guard.os, 'getpgrp', process_guard.os.getpid)
    monkeypatch.setattr(process_guard.os, 'close', lambda _: None)
    monkeypatch.setattr(process_guard.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(process_guard.select, 'select', lambda *_: ([], [], []))
    monkeypatch.setattr(process_guard.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr('sys.argv', ['guard', '--watch', '10', '--done', '11',
                                    '--grace', '1', '--signal', '2', '--', 'synthetic'])
    assert process_guard.main() == -signal.SIGBUS
    assert 'SIGBUS' in capsys.readouterr().out
