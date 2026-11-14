import queue
import signal
from unittest.mock import Mock

import pytest

from core.supervisor import ignore_repeated_sigint
from tests.test_supervisor import FakeThread, make_supervisor


def test_first_sigint_requests_graceful_shutdown():
    previous = signal.getsignal(signal.SIGINT)

    with ignore_repeated_sigint() as stop_requested:
        assert not stop_requested.is_set()

        signal.raise_signal(signal.SIGINT)

        assert stop_requested.is_set()

    assert signal.getsignal(signal.SIGINT) == previous


def test_second_sigint_interrupts():
    with ignore_repeated_sigint() as stop_requested:
        handler = signal.getsignal(signal.SIGINT)

        handler(signal.SIGINT, None)
        assert stop_requested.is_set()

        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)


def test_supervisor_stops_after_first_sigint():
    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    def fake_get(timeout=None):
        signal.raise_signal(signal.SIGINT)
        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    supervisor.run()

    supervisor.frames.get.assert_called_once()
    log.info.assert_any_call("Зупинено користувачем.")
