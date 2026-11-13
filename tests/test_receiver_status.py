from contextlib import contextmanager
from unittest.mock import Mock

from services.receiver_status import (
    init_receiver_db,
    record_heartbeat,
    mark_stopped,
    read_receiver_status,
    receiver_heartbeat,
)


def test_missing_table_means_offline(tmp_path):
    path = tmp_path / "new.db"
    assert read_receiver_status(path) == "offline"


def test_fresh_and_stale_heartbeat(tmp_path):
    path = tmp_path / "status.db"
    init_receiver_db(path)
    record_heartbeat(path, "session1", now=100)

    assert read_receiver_status(path, now=105) == "listening"
    assert read_receiver_status(path, now=111) == "offline"


def test_stopped_receiver_is_offline(tmp_path):
    path = tmp_path / "status.db"
    init_receiver_db(path)
    record_heartbeat(path, "session1")

    mark_stopped(path, "session1")

    assert read_receiver_status(path) == "offline"


def test_old_instance_cannot_stop_new_instance(tmp_path):
    path = tmp_path / "status.db"
    init_receiver_db(path)

    record_heartbeat(path, "old")
    record_heartbeat(path, "new")
    mark_stopped(path, "old")

    assert read_receiver_status(path) == "listening"


def test_heartbeat_context_lifecycle(tmp_path):
    path = tmp_path / "status.db"

    with receiver_heartbeat(path, interval=30):
        assert read_receiver_status(path) == "listening"

    assert read_receiver_status(path) == "offline"


def test_future_timestamp_is_not_trusted(tmp_path):
    path = tmp_path / "status.db"
    init_receiver_db(path)
    record_heartbeat(path, "session1", now=200)

    assert read_receiver_status(path, now=100) == "offline"
