from services.bot_state import BotState


def test_initial_state():
    state = BotState()

    assert state.get_notify_mode() == "all"


def test_change_notify_mode():
    state = BotState()

    with state.lock:
        state.set_notify_mode("watch")

    with state.lock:
        assert state.get_notify_mode() == "watch"

    with state.lock:
        state.set_notify_mode("commands")

    assert state.get_notify_mode() == "commands"


def test_states_are_independent():
    first = BotState()
    second = BotState()

    with first.lock:
        first.set_notify_mode("watch")

    assert second.get_notify_mode() == "all"
