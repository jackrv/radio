from threading import RLock
from unittest.mock import Mock

from core.event_processor import EventProcessor


class FakeDictionary:
    def __init__(self, items):
        self.items = items

    def find(self, text):
        return self.items.get(text, [])


def make_processor(
    mode="all",
    callsigns=None,
    commands=None,
    watch=None,
    short_garbage=None,
    log_callsigns_only=True,
    fresh=None,
    rollback=None,
):
    callsigns = callsigns or FakeDictionary({})
    commands = commands or FakeDictionary({})
    watch = watch or FakeDictionary({})

    save_event = Mock(return_value=42)
    telegram = Mock()
    telegram.register_phrase_for_fix.return_value = "fix123"
    log = Mock()
    lock = RLock()

    processor = EventProcessor(
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        short_garbage=short_garbage or set(),
        save_event=save_event,
        telegram_service=telegram,
        fresh=fresh or Mock(return_value=True),
        rollback=rollback,
        state_lock=lock,
        get_notify_mode=lambda: mode,
        db_path="test.db",
        log_callsigns_only=log_callsigns_only,
        log=log,
    )

    return processor, save_event, telegram, log


def test_all_mode_sends_notification():
    processor, save_event, telegram, _ = make_processor()

    processor.process("Обычная фраза", b"pcm")

    telegram.register_phrase_for_fix.assert_called_once_with(
        "Обычная фраза", None
    )
    telegram.notify.assert_called_once_with(
        "Эфир", "", "", "Обычная фраза", b"pcm", fix_id="fix123"
    )
    save_event.assert_not_called()


def test_watch_mode_notifies_only_watch_matches():
    watch = FakeDictionary({"Наблюдаемая фраза": [("наблюдение", None, None)]})
    processor, _, telegram, _ = make_processor(
        mode="watch",
        watch=watch,
    )

    processor.process("Обычная фраза", b"pcm")
    telegram.notify.assert_not_called()

    processor.process("Наблюдаемая фраза", b"pcm")
    telegram.notify.assert_called_once()


def test_commands_mode_notifies_on_command():
    commands = FakeDictionary({"Команда": [("команда", None, None)]})
    processor, _, telegram, _ = make_processor(
        mode="commands",
        commands=commands,
    )

    processor.process("Обычная фраза", b"pcm")
    telegram.notify.assert_not_called()

    processor.process("Команда", b"pcm")
    telegram.notify.assert_called_once()


def test_event_is_saved_and_id_passed_to_telegram():
    commands = FakeDictionary({"Команда": [("команда", None, None)]})
    processor, save_event, telegram, _ = make_processor(
        commands=commands,
    )

    processor.process("Команда", b"pcm")

    save_event.assert_called_once_with(
        "", "команда", "", "", "Команда", "test.db"
    )
    telegram.register_phrase_for_fix.assert_called_once_with(
        "Команда", 42
    )


def test_database_and_telegram_have_separate_cooldowns():
    commands = FakeDictionary({"Команда": [("команда", None, None)]})
    fresh = Mock(side_effect=lambda key: key[0] == "tg")

    processor, save_event, telegram, _ = make_processor(
        commands=commands,
        fresh=fresh,
    )

    processor.process("Команда", b"pcm")

    save_event.assert_not_called()
    telegram.notify.assert_called_once()
    assert fresh.call_args_list[0].args == (("db", "", "команда"),)
    assert fresh.call_args_list[1].args == (("tg", "Команда"),)


def test_short_garbage_is_discarded():
    processor, save_event, telegram, log = make_processor(
        short_garbage={"мусор"},
    )

    processor.process("мусор", b"pcm")

    save_event.assert_not_called()
    telegram.notify.assert_not_called()
    log.info.assert_called_once()


def test_callsign_is_not_saved_when_logging_disabled():
    callsigns = FakeDictionary({"Позывной": [("позывной", None, None)]})
    processor, save_event, _, _ = make_processor(
        callsigns=callsigns,
        log_callsigns_only=False,
    )

    processor.process("Позывной", b"pcm")

    save_event.assert_not_called()


def test_sqlite_error_does_not_block_telegram():
    commands = FakeDictionary({
        "Команда": [("команда", None, None)]
    })

    processor, save_event, telegram, log = make_processor(
        commands=commands
    )

    save_event.side_effect = RuntimeError(
        "SQLite database is locked"
    )

    # Ошибка SQLite не должна прерывать обработку события.
    processor.process("Команда", b"pcm")

    save_event.assert_called_once()

    # Событие не сохранилось, поэтому event_id отсутствует.
    telegram.register_phrase_for_fix.assert_called_once_with(
        "Команда", None
    )

    # Telegram должен получить уведомление.
    telegram.notify.assert_called_once_with(
        "Эфир", "", "", "Команда", b"pcm", fix_id="fix123"
    )

    # Ошибка SQLite должна попасть в журнал.
    log.exception.assert_called_once()


def test_telegram_error_does_not_affect_sqlite():
    commands = FakeDictionary({
        "Команда": [("команда", None, None)]
    })

    processor, save_event, telegram, log = make_processor(
        commands=commands
    )

    telegram.notify.side_effect = RuntimeError(
        "Telegram connection error"
    )

    # Ошибка Telegram не должна выходить из EventProcessor.
    processor.process("Команда", b"pcm")

    # Запись в SQLite должна быть выполнена.
    save_event.assert_called_once_with(
        "", "команда", "", "", "Команда", "test.db"
    )

    telegram.register_phrase_for_fix.assert_called_once_with(
        "Команда", 42
    )

    telegram.notify.assert_called_once()

    # Ошибка Telegram должна попасть в журнал.
    log.exception.assert_called_once()


def test_sqlite_failure_allows_immediate_retry(monkeypatch):
    from core.cooldown import Cooldown
    import core.cooldown as cooldown_module

    # Фиксируем время: между попытками не прошло ни секунды.
    monkeypatch.setattr(
        cooldown_module.time,
        "monotonic",
        lambda: 100.0,
    )

    cooldown = Cooldown(3.0)

    commands = FakeDictionary({
        "Команда": [("команда", None, None)]
    })

    processor, save_event, telegram, log = make_processor(
        mode="watch",
        commands=commands,
        fresh=cooldown.fresh,
        rollback=cooldown.rollback,
    )

    # Первая запись завершится ошибкой, вторая должна пройти.
    save_event.side_effect = [
        RuntimeError("SQLite database is locked"),
        42,
    ]

    processor.process("Команда", b"pcm")
    processor.process("Команда", b"pcm")

    assert save_event.call_count == 2
    log.exception.assert_called_once()
    telegram.notify.assert_not_called()
