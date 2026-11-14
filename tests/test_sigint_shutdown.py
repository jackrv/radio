import signal

import pytest

from core.supervisor import ignore_repeated_sigint


def test_sigint_handler_is_restored():
    original = signal.getsignal(signal.SIGINT)

    with ignore_repeated_sigint():
        assert signal.getsignal(signal.SIGINT) is not original

    assert signal.getsignal(signal.SIGINT) is original


def test_first_sigint_requests_stop_and_second_interrupts():
    original = signal.getsignal(signal.SIGINT)

    with ignore_repeated_sigint() as stop_requested:
        handler = signal.getsignal(signal.SIGINT)

        # Первое Ctrl+C запрашивает штатную остановку.
        handler(signal.SIGINT, None)

        assert stop_requested.is_set()

        # Второе Ctrl+C прерывает затянувшееся ожидание.
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)

    # После выхода восстанавливается исходный обработчик.
    assert signal.getsignal(signal.SIGINT) is original
