import pytest

from services.receiver_control import (
    read_pause_state,
    set_pause_minutes,
)


def test_default_state(tmp_path):
    db = tmp_path / "receiver.db"

    state = read_pause_state(db, now=1000)

    assert state == {
        "pause_requested": False,
        "resume_at": None,
        "remaining_seconds": 0,
        "revision": 0,
    }


def test_pause_for_15_minutes(tmp_path):
    db = tmp_path / "receiver.db"

    state = set_pause_minutes(db, 15, now=1000)

    assert state["pause_requested"] is True
    assert state["resume_at"] == 1900
    assert state["remaining_seconds"] == 900


def test_pause_expires_automatically(tmp_path):
    db = tmp_path / "receiver.db"

    set_pause_minutes(db, 5, now=1000)

    assert read_pause_state(
        db, now=1299
    )["pause_requested"] is True

    assert read_pause_state(
        db, now=1300
    )["pause_requested"] is False


def test_manual_resume(tmp_path):
    db = tmp_path / "receiver.db"

    set_pause_minutes(db, 60, now=1000)
    state = set_pause_minutes(db, 0, now=1100)

    assert state["pause_requested"] is False
    assert state["remaining_seconds"] == 0


def test_new_pause_replaces_previous_timer(tmp_path):
    db = tmp_path / "receiver.db"

    set_pause_minutes(db, 60, now=1000)
    state = set_pause_minutes(db, 5, now=1100)

    assert state["resume_at"] == 1400
    assert state["remaining_seconds"] == 300


@pytest.mark.parametrize(
    "minutes",
    [-1, 241, 1.5, True, "15"],
)
def test_invalid_pause_rejected(tmp_path, minutes):
    db = tmp_path / "receiver.db"

    with pytest.raises(ValueError):
        set_pause_minutes(db, minutes, now=1000)


def test_revision_increases_on_pause_and_resume(tmp_path):
    db = tmp_path / "version.db"

    first = set_pause_minutes(
        db, 15, now=1000, expected_revision=0
    )
    assert first["revision"] == 1

    resumed = set_pause_minutes(
        db, 0, now=1100, expected_revision=1
    )
    assert resumed["revision"] == 2
    assert resumed["pause_requested"] is False


def test_existing_database_preserves_pause(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"

    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE receiver_control (
                id INTEGER PRIMARY KEY CHECK (id=1),
                pause_until REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO receiver_control VALUES (1,1300)"
        )

    state = read_pause_state(db, now=1000)

    assert state["pause_requested"] is True
    assert state["remaining_seconds"] == 300
    assert state["revision"] == 0

    updated = set_pause_minutes(
        db, 0, now=1000, expected_revision=0
    )
    assert updated["revision"] == 1


def test_stale_revision_cannot_overwrite_pause(tmp_path):
    from services.receiver_control import PauseVersionConflict

    db = tmp_path / "conflict.db"

    set_pause_minutes(db, 15, now=1000, expected_revision=0)

    with pytest.raises(PauseVersionConflict):
        set_pause_minutes(
            db, 30, now=1001, expected_revision=0
        )

    state = read_pause_state(db, now=1001)
    assert state["revision"] == 1
    assert state["remaining_seconds"] == 899


def test_parallel_commands_allow_only_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from services.receiver_control import PauseVersionConflict

    db = tmp_path / "parallel.db"
    version = read_pause_state(db, now=1000)["revision"]

    def send(minutes):
        try:
            set_pause_minutes(
                db,
                minutes,
                now=1000,
                expected_revision=version,
            )
            return "accepted"
        except PauseVersionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, (5, 15)))

    assert results.count("accepted") == 1
    assert results.count("conflict") == 1
    assert read_pause_state(db, now=1000)["revision"] == 1
