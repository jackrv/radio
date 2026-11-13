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
