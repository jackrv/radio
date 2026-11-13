import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from services.telegram import TelegramService


def make_service():
    return TelegramService(
        bot_token="",
        chat_id="123",
        sample_rate=16000,
        db_path="unused-test.db",
        log=Mock(),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )


class CheckedDict(OrderedDict):

    def __init__(self, lock):
        self.lock = lock
        super().__init__()

    def __setitem__(self, key, value):
        assert self.lock.locked(), "Запись без блокировки"
        return super().__setitem__(key, value)

    def __len__(self):
        assert self.lock.locked(), "Размер без блокировки"
        return super().__len__()

    def get(self, key, default=None):
        assert self.lock.locked(), "Чтение без блокировки"
        return super().get(key, default)

    def popitem(self, last=True):
        assert self.lock.locked(), "Удаление без блокировки"
        return super().popitem(last=last)


def test_phrase_operations_are_locked():
    service = make_service()

    service.recent_phrases = CheckedDict(
        service._phrases_lock
    )
    service.recent_phrases_max = 2

    first = service.register_phrase_for_fix(
        "Липка 700", None
    )
    second = service.register_phrase_for_fix(
        "Наган 110", None
    )

    assert service.resolve_phrase_by_fix_id(first) == (
        "Липка 700", None
    )

    third = service.register_phrase_for_fix(
        "Наган 112", None
    )

    assert service.resolve_phrase_by_fix_id(first) == (
        None, None
    )
    assert service.resolve_phrase_by_fix_id(second) == (
        "Наган 110", None
    )
    assert service.resolve_phrase_by_fix_id(third) == (
        "Наган 112", None
    )

    with service._phrases_lock:
        assert len(service.recent_phrases) == 2


def test_concurrent_phrase_access():
    service = make_service()

    service.recent_phrases = CheckedDict(
        service._phrases_lock
    )
    service.recent_phrases_max = 200

    def worker(index):
        text = f"Фраза {index}"

        fix_id = service.register_phrase_for_fix(
            text, None
        )

        result, event_id = (
            service.resolve_phrase_by_fix_id(fix_id)
        )

        assert event_id is None
        assert result is None or result == text

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(400)))

    with service._phrases_lock:
        assert len(service.recent_phrases) == 200
