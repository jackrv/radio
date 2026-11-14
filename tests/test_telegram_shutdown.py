import logging
import threading

from services.telegram import TelegramService


def make_service():
    return TelegramService(
        bot_token="test-token",
        chat_id="123",
        sample_rate=16000,
        db_path=":memory:",
        log=logging.getLogger("test_telegram_shutdown"),
        state_lock=threading.Lock(),
        waiting_for_correction={},
        get_notify_mode=lambda: "all",
        set_notify_mode=lambda mode: None,
    )


def notify(service, number):
    service.notify(
        "Эфир", "", "",
        f"Сообщение {number}",
        b"\x00\x00",
    )


def test_shutdown_empty_queue():
    service = make_service()

    assert service.shutdown(timeout=0.1) is True
    assert service._outgoing_stop.is_set()
    assert service._outgoing_thread is None


def test_shutdown_flushes_pending_messages():
    service = make_service()
    started = threading.Event()
    release = threading.Event()
    sent = []
    result = []

    def fake_send(pcm, caption, fix_id):
        started.set()
        assert release.wait(timeout=5)
        sent.append(caption)

    service.send_voice = fake_send

    notify(service, 1)
    assert started.wait(timeout=3)
    notify(service, 2)

    stopper = threading.Thread(
        target=lambda: result.append(
            service.shutdown(timeout=3)
        )
    )

    try:
        stopper.start()
        assert service._outgoing_stop.wait(timeout=2)
    finally:
        release.set()
        stopper.join(timeout=5)

    assert not stopper.is_alive()
    assert result == [True]
    assert len(sent) == 2
    assert service._outgoing_queue.unfinished_tasks == 0
    assert not service._outgoing_thread.is_alive()


def test_shutdown_timeout(caplog):
    service = make_service()
    started = threading.Event()
    release = threading.Event()

    def fake_send(pcm, caption, fix_id):
        started.set()
        release.wait(timeout=5)

    service.send_voice = fake_send

    try:
        notify(service, 1)
        assert started.wait(timeout=3)

        with caplog.at_level(logging.WARNING):
            result = service.shutdown(timeout=0.01)

        assert result is False
        assert "незавершеними" in caplog.text

    finally:
        release.set()
        service._outgoing_thread.join(timeout=3)

    assert not service._outgoing_thread.is_alive()


def test_shutdown_rejects_new_notifications(caplog):
    service = make_service()
    sent = []

    service.send_voice = lambda *args: sent.append(args)

    assert service.shutdown(timeout=0.1) is True

    with caplog.at_level(logging.WARNING):
        notify(service, 1)

    assert sent == []
    assert service._outgoing_queue.qsize() == 0
    assert service._outgoing_thread is None
    assert "сповіщення пропущено" in caplog.text
