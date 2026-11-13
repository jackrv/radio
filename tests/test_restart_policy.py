import pytest

from core.restart_policy import RestartPolicy


def make_policy():
    now = [100.0]
    policy = RestartPolicy(
        delays=(5.0, 15.0, 45.0),
        clock=lambda: now[0],
    )
    return policy, now


def test_first_restart_after_5_seconds():
    policy, now = make_policy()

    assert policy.should_restart() is False
    assert policy.attempts == 0

    now[0] = 104.9
    assert policy.should_restart() is False

    now[0] = 105.0
    assert policy.should_restart() is True
    assert policy.attempts == 1


def test_second_restart_after_15_seconds():
    policy, now = make_policy()

    policy.should_restart()
    now[0] = 105.0
    assert policy.should_restart() is True

    assert policy.should_restart() is False

    now[0] = 119.9
    assert policy.should_restart() is False

    now[0] = 120.0
    assert policy.should_restart() is True
    assert policy.attempts == 2


def test_third_restart_after_45_seconds():
    policy, now = make_policy()

    policy.should_restart()

    now[0] = 105.0
    assert policy.should_restart() is True
    assert policy.should_restart() is False

    now[0] = 120.0
    assert policy.should_restart() is True
    assert policy.should_restart() is False

    now[0] = 164.9
    assert policy.should_restart() is False

    now[0] = 165.0
    assert policy.should_restart() is True
    assert policy.attempts == 3


def test_no_restart_after_three_attempts():
    policy, now = make_policy()

    for timestamp in (100.0, 105.0, 120.0, 165.0):
        now[0] = timestamp
        policy.should_restart()
        if timestamp != 165.0:
            policy.should_restart()

    assert policy.exhausted is True
    assert policy.attempts == 3

    now[0] = 1000.0
    assert policy.should_restart() is False


def test_invalid_delays():
    with pytest.raises(ValueError):
        RestartPolicy(delays=())

    with pytest.raises(ValueError):
        RestartPolicy(delays=(0, 5, 10))

    with pytest.raises(ValueError):
        RestartPolicy(delays=(-1, 5, 10))


def test_reset_restores_restart_attempts():
    policy, now = make_policy()

    # Первая авария и перезапуск через 5 секунд.
    assert policy.should_restart() is False

    now[0] = 105.0
    assert policy.should_restart() is True
    assert policy.attempts == 1

    # Запланирована следующая попытка через 15 секунд.
    assert policy.should_restart() is False
    assert policy.next_retry_at == 120.0

    # После длительной стабильной работы Supervisor
    # должен иметь возможность сбросить политику.
    policy.reset()

    assert policy.attempts == 0
    assert policy.next_retry_at is None
    assert policy.exhausted is False

    # После сброса задержки начинаются снова с 5 секунд.
    now[0] = 2000.0
    assert policy.should_restart() is False

    now[0] = 2005.0
    assert policy.should_restart() is True
    assert policy.attempts == 1
