from core.cooldown import Cooldown


def test_first_call_is_fresh():
    cooldown = Cooldown(3.0)

    assert cooldown.fresh(("db", "event")) is True


def test_repeated_call_is_blocked():
    cooldown = Cooldown(3.0)

    assert cooldown.fresh(("tg", "message")) is True
    assert cooldown.fresh(("tg", "message")) is False


def test_different_keys_are_independent():
    cooldown = Cooldown(3.0)

    assert cooldown.fresh(("db", "event")) is True
    assert cooldown.fresh(("tg", "message")) is True


def test_expired_cooldown_allows_call(monkeypatch):
    import core.cooldown as module

    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    cooldown = Cooldown(3.0)

    assert cooldown.fresh("event") is True

    now[0] = 104.0

    assert cooldown.fresh("event") is True


def test_rollback_allows_immediate_retry(monkeypatch):
    import core.cooldown as module

    monkeypatch.setattr(
        module.time, "monotonic", lambda: 100.0
    )

    cooldown = Cooldown(3.0)
    key = ("db", "команда")

    assert cooldown.fresh(key) is True
    assert cooldown.fresh(key) is False

    cooldown.rollback(key)

    assert cooldown.fresh(key) is True
    assert cooldown.fresh(key) is False
