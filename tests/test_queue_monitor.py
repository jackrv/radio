from unittest.mock import Mock

import pytest

from core.queue_monitor import QueueDropMonitor


class FakeQueue:
    def __init__(self, dropped=0):
        self.dropped = dropped


def make_monitor(interval_s=30.0):
    now = [100.0]
    frames = FakeQueue()
    segments = FakeQueue()
    log = Mock()

    monitor = QueueDropMonitor(
        frames=frames,
        segments=segments,
        log=log,
        interval_s=interval_s,
        clock=lambda: now[0],
    )

    return monitor, frames, segments, log, now


def test_no_warning_without_losses():
    monitor, frames, segments, log, now = make_monitor()

    now[0] = 130.0
    monitor.check()

    log.warning.assert_not_called()


def test_check_before_interval():
    monitor, frames, segments, log, now = make_monitor()

    frames.dropped = 5
    now[0] = 120.0

    monitor.check()

    log.warning.assert_not_called()


def test_reports_frame_losses():
    monitor, frames, segments, log, now = make_monitor()

    frames.dropped = 10
    now[0] = 130.0

    monitor.check()

    log.warning.assert_called_once()
    assert log.warning.call_args.args[1:] == (
        10, 0, 10, 0
    )


def test_reports_segment_losses():
    monitor, frames, segments, log, now = make_monitor()

    segments.dropped = 3
    now[0] = 130.0

    monitor.check()

    log.warning.assert_called_once()
    assert log.warning.call_args.args[1:] == (
        0, 3, 0, 3
    )


def test_reports_only_new_losses():
    monitor, frames, segments, log, now = make_monitor()

    frames.dropped = 10
    now[0] = 130.0
    monitor.check()

    log.warning.reset_mock()

    frames.dropped = 12
    segments.dropped = 1
    now[0] = 160.0
    monitor.check()

    log.warning.assert_called_once()

    assert log.warning.call_args.args[1:] == (
        2, 1, 12, 1
    )


def test_invalid_interval():
    with pytest.raises(ValueError):
        QueueDropMonitor(
            FakeQueue(),
            FakeQueue(),
            Mock(),
            interval_s=0,
        )
