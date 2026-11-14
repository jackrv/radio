import core.cooldown as module
from core.cooldown import Cooldown


def test_first_call_at_monotonic_zero(monkeypatch):
    monkeypatch.setattr(
        module.time, "monotonic", lambda: 0.0
    )

    cooldown = Cooldown(3.0)

    assert cooldown.fresh("first") is True
    assert cooldown.fresh("first") is False


def test_old_entries_are_removed(monkeypatch):
    now = [100.0]

    monkeypatch.setattr(
        module.time, "monotonic", lambda: now[0]
    )

    cooldown = Cooldown(3.0)

    # Имитируем большое количество уникальных фраз.
    for index in range(2000):
        assert cooldown.fresh(
            ("tg", f"фраза {index}")
        )

    assert len(cooldown.last_seen) == 2000

    # Все старые записи должны исчезнуть.
    now[0] = 104.0

    assert cooldown.fresh(("tg", "новая фраза"))

    assert len(cooldown.last_seen) == 1
    assert len(cooldown._timeline) == 1


def test_long_stream_does_not_accumulate_old_keys(
    monkeypatch
):
    now = [100.0]

    monkeypatch.setattr(
        module.time, "monotonic", lambda: now[0]
    )

    cooldown = Cooldown(3.0)

    # 5000 разных сообщений с интервалом 250 мс.
    for index in range(5000):
        assert cooldown.fresh(
            ("db", f"message_{index}")
        )

        assert len(cooldown.last_seen) <= 13
        assert len(cooldown._timeline) <= 13

        now[0] += 0.25

    # При таком потоке хранятся только последние секунды,
    # а не все 5000 сообщений.
    assert len(cooldown.last_seen) <= 13


def test_rollback_does_not_erase_new_registration(
    monkeypatch
):
    now = [100.0]

    monkeypatch.setattr(
        module.time, "monotonic", lambda: now[0]
    )

    cooldown = Cooldown(3.0)

    assert cooldown.fresh("команда") is True

    # Помилка SQLite: разрешаем повторную попытку.
    cooldown.rollback("команда")

    now[0] = 101.0

    assert cooldown.fresh("команда") is True

    # Время первой регистрации истекло,
    # но повторная регистрация ещё действительна.
    now[0] = 103.0

    assert cooldown.fresh("другое событие") is True
    assert cooldown.fresh("команда") is False

    # Теперь истёк срок второй регистрации.
    now[0] = 104.0

    assert cooldown.fresh("команда") is True
