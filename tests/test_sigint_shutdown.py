import signal

import pytest

from core.supervisor import ignore_repeated_sigint


def test_sigint_handler_is_restored():
    original = signal.getsignal(signal.SIGINT)

    with ignore_repeated_sigint():
        assert signal.getsignal(signal.SIGINT) is not original

    assert signal.getsignal(signal.SIGINT) is original


def test_second_sigint_during_shutdown_is_ignored():
    original = signal.getsignal(signal.SIGINT)

    with ignore_repeated_sigint():
        handler = signal.getsignal(signal.SIGINT)

        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)

        # Повторное прерывание не должно вызвать исключение.
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)

    assert signal.getsignal(signal.SIGINT) is original
