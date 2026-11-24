import logging
import threading
import time
from unittest.mock import Mock

from services.telegram import TelegramService

RECIPIENTS = [{"chat_id": 1001, "role": "admin", "mode": "all"}]


def make_service():
    service = TelegramService(
        bot_token="test-token",
        sample_rate=16000,
        db_path=":memory:",
        log=logging.getLogger("test_telegram_outbox"),
    )
    # Ці тести перевіряють транспорт і чергу; авторизація має окремі інтеграційні тести.
    service._user_state = lambda user_id: ("admin", "all")
    return service


def notify(service, number):
    service.notify(
        "Эфир",
        "",
        "",
        f"Сообщение {number}",
        b"\x00\x00",
        recipients=RECIPIENTS,
    )


def test_outbox_fifo_and_single_worker():
    service = make_service()
    sent = []
    worker_ids = []

    def fake_send(pcm, caption, fix_id, recipients, **_):
        assert recipients == RECIPIENTS
        sent.append(caption)
        worker_ids.append(threading.get_ident())

    service.send_voice = fake_send

    for i in range(5):
        notify(service, i)

    service._outgoing_queue.join()

    assert len(sent) == 5
    assert all(
        f"Сообщение {i}" in caption
        for i, caption in enumerate(sent)
    )
    assert len(set(worker_ids)) == 1
    assert service._outgoing_thread.is_alive()


def test_outbox_overflow_does_not_block(caplog):
    service = make_service()

    started = threading.Event()
    release = threading.Event()
    sent = []

    def fake_send(pcm, caption, fix_id, recipients, **_):
        if not started.is_set():
            started.set()
            release.wait(timeout=5)
        sent.append(caption)

    service.send_voice = fake_send

    try:
        notify(service, 0)
        assert started.wait(timeout=3)

        # Один вызов уже выполняется, 32 ожидают.
        for i in range(1, 33):
            notify(service, i)

        assert service._outgoing_queue.qsize() == 32

        with caplog.at_level(logging.ERROR):
            notify(service, 33)

        assert "переповнено" in caplog.text

    finally:
        release.set()

    service._outgoing_queue.join()

    assert len(sent) == 33
    assert not any("Сообщение 33" in x for x in sent)


def test_worker_survives_send_exception(caplog):
    service = make_service()
    attempts = []

    def fake_send(pcm, caption, fix_id, recipients, **_):
        attempts.append(caption)

        if "Сообщение 1" in caption:
            raise RuntimeError("Тестовый сбой")

    service.send_voice = fake_send

    with caplog.at_level(logging.ERROR):
        notify(service, 1)
        notify(service, 2)
        service._outgoing_queue.join()

    assert len(attempts) == 2
    assert "Помилка фонового надсилання" in caplog.text
    assert service._outgoing_thread.is_alive()


def _ok(file_id):
    reply = Mock(status_code=200)
    reply.json.return_value = {"ok": True, "result": {"voice": {"file_id": file_id}}}
    return reply


def test_hung_recipient_does_not_delay_others(monkeypatch):
    """Зависле завантаження першому отримувачу не тримає другого й наступні фрази."""
    import shared.telegram_outbox as outbox

    monkeypatch.setattr(outbox, "SHARED_UPLOAD_WAIT_S", 0.2)
    service = make_service()
    service.pcm_to_ogg = lambda pcm: b"OggS"
    release = threading.Event()
    delivered = []

    def request(method, operation, **kwargs):
        chat = kwargs["data"]["chat_id"]
        if chat == 1001:
            assert release.wait(timeout=5)
        delivered.append((chat, kwargs["data"]["caption"].split("\n")[-1],
                          "voice" in kwargs.get("files", {})))
        return _ok(f"FILE-{chat}")

    service._request = request
    recipients = RECIPIENTS + [{"chat_id": 2002, "role": "admin", "mode": "all"}]
    try:
        service.notify("Ефір", "", "", "Фраза 1", b"\0\0", recipients=recipients)
        service.notify("Ефір", "", "", "Фраза 2", b"\0\0", recipients=recipients)
        deadline = time.monotonic() + 3
        while len(delivered) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        # Другий отримувач отримав обидві фрази по порядку, поки перший висить;
        # file_id не дочекався, тож завантажив голосове сам.
        assert delivered == [(2002, "Фраза 1", True), (2002, "Фраза 2", True)]
    finally:
        release.set()
        assert service.shutdown(timeout=3)
    assert [d[:2] for d in delivered if d[0] == 1001] == [(1001, "Фраза 1"), (1001, "Фраза 2")]


def test_full_recipient_queue_skips_only_that_recipient(caplog):
    import shared.telegram_outbox as outbox

    service = make_service()
    release = threading.Event()
    ran = []

    def blocking():
        release.wait(timeout=5)

    try:
        assert service._dispatch(1001, blocking) is not None
        for _ in range(outbox.QUEUE_SIZE + 1):
            service._dispatch(1001, lambda: ran.append(1001))
        with caplog.at_level(logging.ERROR):
            assert service._dispatch(1001, lambda: ran.append(1001)) is None
        assert "Черга отримувача переповнена" in caplog.text
        done = service._dispatch(2002, lambda: ran.append(2002))
        assert done.wait(timeout=2) and 2002 in ran
    finally:
        release.set()
        assert service.shutdown(timeout=3)
