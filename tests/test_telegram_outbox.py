import logging
import threading

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

    def fake_send(pcm, caption, fix_id, recipients):
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

    def fake_send(pcm, caption, fix_id, recipients):
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

    def fake_send(pcm, caption, fix_id, recipients):
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
