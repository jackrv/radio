from services.bot_state import BotState


def test_initial_state():
    state = BotState()

    assert state.get_notify_mode() == "all"
    assert state.waiting_for_correction == {}


def test_change_notify_mode():
    state = BotState()

    with state.lock:
        state.set_notify_mode("watch")

    with state.lock:
        assert state.get_notify_mode() == "watch"

    with state.lock:
        state.set_notify_mode("commands")

    assert state.get_notify_mode() == "commands"


def test_waiting_for_correction():
    state = BotState()

    with state.lock:
        state.waiting_for_correction[123] = {
            "event_id": 42,
            "raw_text": "Тестовое сообщение",
        }

    with state.lock:
        pending = state.waiting_for_correction.pop(123, None)

    assert pending == {
        "event_id": 42,
        "raw_text": "Тестовое сообщение",
    }
    assert state.waiting_for_correction == {}


def test_states_are_independent():
    first = BotState()
    second = BotState()

    with first.lock:
        first.set_notify_mode("watch")

    assert second.get_notify_mode() == "all"
    assert first.waiting_for_correction is not second.waiting_for_correction
