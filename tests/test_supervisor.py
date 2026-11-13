import queue
from unittest.mock import Mock

from core.supervisor import ApplicationSupervisor


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True


class FakeThread:
    instances = []

    def __init__(self, target, daemon):
        self.target = target
        self.daemon = daemon
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class FakeSegmenter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def process(self, frame):
        if frame == b"speech":
            return b"completed segment"
        return None


def make_supervisor(frames):
    segments = queue.Queue()
    log = Mock()

    supervisor = ApplicationSupervisor(
        frames=frames,
        segments=segments,
        on_audio=Mock(),
        transcriber=Mock(),
        telegram_listener=Mock(),
        sample_rate=16000,
        frame_ms=30,
        frame_len=480,
        vad_level=2,
        preroll_ms=500,
        silence_end_ms=400,
        min_speech_ms=500,
        max_segment_s=20,
        log=log,
        stream_factory=FakeStream,
        segmenter_factory=FakeSegmenter,
        thread_factory=FakeThread,
    )

    return supervisor, segments, log


def test_supervisor_starts_background_threads():
    FakeThread.instances = []

    supervisor, _, _ = make_supervisor(queue.Queue())
    supervisor.frames.get = Mock(side_effect=KeyboardInterrupt)

    supervisor.run()

    assert len(FakeThread.instances) == 2
    assert all(thread.daemon for thread in FakeThread.instances)
    assert all(thread.started for thread in FakeThread.instances)


def test_supervisor_processes_audio_segments():
    FakeThread.instances = []

    class FakeFrames:
        def __init__(self):
            self.items = [b"noise", b"speech"]

        def get(self, timeout=None):
            if not self.items:
                raise KeyboardInterrupt
            return self.items.pop(0)

    frames = FakeFrames()
    supervisor, segments, _ = make_supervisor(queue.Queue())
    supervisor.frames = frames

    supervisor.run()

    assert segments.get_nowait() == b"completed segment"
    assert segments.empty()


def test_supervisor_skips_telegram_when_disabled():
    FakeThread.instances = []

    supervisor, _, _ = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None
    supervisor.frames.get = Mock(side_effect=KeyboardInterrupt)

    supervisor.run()

    assert len(FakeThread.instances) == 1
    assert FakeThread.instances[0].target == supervisor.transcriber


def test_supervisor_closes_audio_stream_on_keyboard_interrupt():
    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.frames.get = Mock(side_effect=KeyboardInterrupt)

    supervisor.run()

    log.info.assert_called_once_with("Остановлено пользователем.")


def test_monitor_checks_when_audio_queue_empty():
    from unittest.mock import patch

    FakeThread.instances = []
    supervisor, segments, log = make_supervisor(queue.Queue())

    supervisor.frames.get = Mock(
        side_effect=[
            queue.Empty(),
            KeyboardInterrupt(),
        ]
    )

    with patch("core.supervisor.QueueDropMonitor") as monitor_class:
        supervisor.run()

    monitor_class.assert_called_once_with(
        supervisor.frames, segments, log
    )

    monitor_class.return_value.check.assert_called_once_with()

    assert supervisor.frames.get.call_count == 2
    supervisor.frames.get.assert_any_call(timeout=1.0)


def test_monitor_checks_after_audio_frame():
    from unittest.mock import patch

    FakeThread.instances = []
    supervisor, segments, log = make_supervisor(queue.Queue())

    supervisor.frames.get = Mock(
        side_effect=[
            b"speech",
            KeyboardInterrupt(),
        ]
    )

    with patch("core.supervisor.QueueDropMonitor") as monitor_class:
        supervisor.run()

    monitor_class.return_value.check.assert_called_once_with()

    assert segments.get_nowait() == b"completed segment"
    supervisor.frames.get.assert_any_call(timeout=1.0)


def test_detects_dead_transcriber_thread_once():
    from unittest.mock import patch

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None

    # Две проверки без аудио, затем остановка теста.
    supervisor.frames.get = Mock(
        side_effect=[
            queue.Empty(),
            queue.Empty(),
            KeyboardInterrupt(),
        ]
    )

    # Имитируем поток, который запустился, но завершился.
    with patch.object(
        FakeThread,
        "is_alive",
        lambda self: False,
        create=True,
    ):
        supervisor.run()

    # Ошибка должна фиксироваться только один раз.
    log.error.assert_called_once_with(
        "Поток распознавания неожиданно завершился"
    )


def test_detects_dead_telegram_thread_once():
    from unittest.mock import patch

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    # Две проверки без аудио, затем завершение теста.
    supervisor.frames.get = Mock(
        side_effect=[
            queue.Empty(),
            queue.Empty(),
            KeyboardInterrupt(),
        ]
    )

    # Whisper работает, Telegram неожиданно остановился.
    def fake_is_alive(thread):
        return (
            thread.started
            and thread.target is not supervisor.telegram_listener
        )

    with patch.object(
        FakeThread,
        "is_alive",
        fake_is_alive,
    ):
        supervisor.run()

    log.error.assert_called_once_with(
        "Поток Telegram неожиданно завершился"
    )


def test_transcriber_restarts_after_delay(monkeypatch):
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([100.0, 104.9, 105.0, 106.0])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 106.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        # Первый поток аварийно завершился.
        # Все последующие потоки работают.
        return (
            thread.started
            and thread is not FakeThread.instances[0]
        )

    from unittest.mock import patch

    with patch.object(
        FakeThread,
        "is_alive",
        fake_is_alive,
    ):
        supervisor.run()

    # Один исходный поток и один перезапущенный.
    assert len(FakeThread.instances) == 2

    assert all(
        thread.target is supervisor.transcriber
        for thread in FakeThread.instances
    )

    log.error.assert_called_once_with(
        "Поток распознавания неожиданно завершился"
    )


def test_whisper_restart_limit(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None
    stream = FakeStream()
    supervisor.stream_factory = lambda **kwargs: stream

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([
        100.0,   # Первый сбой
        104.9,   # Ещё рано
        105.0,   # Перезапуск 1
        106.0,   # Второй сбой
        120.9,   # Ещё рано
        121.0,   # Перезапуск 2
        122.0,   # Третий сбой
        166.9,   # Ещё рано
        167.0,   # Перезапуск 3
        168.0,   # Четвёртый сбой — лимит исчерпан
        999.0,   # Нового перезапуска быть не должно
        1000.0,  # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 1000.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    # Все запущенные потоки имитируют аварийное завершение.
    with patch.object(
        FakeThread,
        "is_alive",
        lambda self: False,
    ):
        from pytest import raises
        with raises(RuntimeError, match="Исчерпаны попытки перезапуска Whisper"):
            supervisor.run()

    # Один первоначальный поток + три перезапуска.
    assert len(FakeThread.instances) == 4

    assert all(
        thread.started
        and thread.target is supervisor.transcriber
        for thread in FakeThread.instances
    )

    # Каждый аварийно завершившийся поток обнаружен.
    assert log.error.call_count == 4

    # После исчерпания лимита только одно предупреждение.
    log.critical.assert_called_once_with(
        "Исчерпаны попытки перезапуска Whisper"
    )

    # Успешно инициированы ровно три перезапуска.
    restart_logs = [
        call for call in log.info.call_args_list
        if call.args
        and call.args[0] == "Поток Whisper перезапущен, попытка %d"
    ]

    assert [call.args[1] for call in restart_logs] == [1, 2, 3]

    # Аудиоустройство освобождено после отказа.
    assert stream.closed is True


def test_whisper_restart_start_failure(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    class FailingThread(FakeThread):
        def start(self):
            raise RuntimeError("Thread start failed")

    def thread_factory(target, daemon):
        # Второй созданный поток не сможет запуститься.
        if len(FakeThread.instances) == 1:
            return FailingThread(target, daemon)

        return FakeThread(target, daemon)

    supervisor.thread_factory = thread_factory

    ticks = iter([
        100.0,
        105.0,
        106.0,
        120.9,
        121.0,
        122.0,
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 122.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        return (
            len(FakeThread.instances) >= 3
            and thread is FakeThread.instances[-1]
            and thread.started
        )

    with patch.object(FakeThread, "is_alive", fake_is_alive):
        supervisor.run()

    # Исходный поток + неудачный запуск + успешный запуск.
    assert len(FakeThread.instances) == 3

    assert FakeThread.instances[0].started
    assert not FakeThread.instances[1].started
    assert FakeThread.instances[2].started

    log.exception.assert_called_once_with(
        "Ошибка перезапуска Whisper"
    )

    log.error.assert_called_once_with(
        "Поток распознавания неожиданно завершился"
    )

    # Следующая попытка после ошибки была успешной.
    assert any(
        call.args == ("Поток Whisper перезапущен, попытка %d", 2)
        for call in log.info.call_args_list
    )

    log.critical.assert_not_called()


def test_whisper_restart_counter_resets_after_30_minutes(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())
    supervisor.telegram_listener = None

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([
        105.0,     # Первый перезапуск
        1904.9,    # Почти 30 минут стабильной работы
        1905.0,    # Ровно 30 минут после перезапуска
        2000.0,    # Вторая авария
        2005.0,    # Ожидаемый перезапуск через 5 секунд
        2006.0,    # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 2006.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        index = FakeThread.instances.index(thread)

        if index == 0:
            return False

        if index == 1:
            return thread.started and now[0] < 2000.0

        return thread.started

    with patch.object(FakeThread, "is_alive", fake_is_alive):
        supervisor.run()

    # Исходный поток + два успешных перезапуска.
    assert len(FakeThread.instances) == 3

    restart_logs = [
        call
        for call in log.info.call_args_list
        if call.args
        and call.args[0] == "Поток Whisper перезапущен, попытка %d"
    ]

    # После 30 минут стабильной работы отсчёт начинается заново.
    assert [call.args[1] for call in restart_logs] == [1, 1]

    log.critical.assert_not_called()


def test_telegram_restarts_after_delay(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    listener = Mock(name="telegram_listener")
    supervisor.telegram_listener = listener

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([
        100.0,   # Обнаружена остановка Telegram
        104.9,   # Перезапускать ещё рано
        105.0,   # Прошло 5 секунд
        106.0,   # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 106.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        # Первый поток Telegram аварийно завершился.
        # Whisper и повторно запущенный Telegram работают.
        return (
            thread.started
            and thread is not FakeThread.instances[1]
        )

    with patch.object(
        FakeThread,
        "is_alive",
        fake_is_alive,
    ):
        supervisor.run()

    # Whisper + первоначальный Telegram + перезапущенный Telegram.
    assert len(FakeThread.instances) == 3

    assert FakeThread.instances[0].target is supervisor.transcriber
    assert FakeThread.instances[1].target is listener
    assert FakeThread.instances[2].target is listener

    assert all(
        thread.started for thread in FakeThread.instances
    )

    log.error.assert_called_once_with(
        "Поток Telegram неожиданно завершился"
    )

    log.critical.assert_not_called()


def test_telegram_restart_limit(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    listener = Mock(name="telegram_listener")
    supervisor.telegram_listener = listener

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([
        100.0,   # Первый сбой Telegram
        104.9,   # Ещё рано
        105.0,   # Перезапуск 1
        106.0,   # Второй сбой
        120.9,   # Ещё рано
        121.0,   # Перезапуск 2
        122.0,   # Третий сбой
        166.9,   # Ещё рано
        167.0,   # Перезапуск 3
        168.0,   # Четвёртый сбой
        999.0,   # Новых перезапусков быть не должно
        1000.0,  # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 1000.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        # Whisper продолжает работать.
        # Все потоки Telegram аварийно завершаются.
        return (
            thread is FakeThread.instances[0]
            and thread.started
        )

    with patch.object(
        FakeThread,
        "is_alive",
        fake_is_alive,
    ):
        supervisor.run()

    # Один Whisper + первоначальный Telegram + 3 перезапуска.
    assert len(FakeThread.instances) == 5

    assert FakeThread.instances[0].target is supervisor.transcriber

    assert all(
        thread.target is listener
        for thread in FakeThread.instances[1:]
    )

    assert all(
        thread.started
        for thread in FakeThread.instances
    )

    # Четыре аварийные остановки Telegram.
    assert log.error.call_count == 4

    log.critical.assert_called_once_with(
        "Исчерпаны попытки перезапуска Telegram"
    )

    # Ровно три попытки восстановления.
    restart_logs = [
        call
        for call in log.info.call_args_list
        if call.args
        and call.args[0] == "Поток Telegram перезапущен, попытка %d"
    ]

    assert [call.args[1] for call in restart_logs] == [1, 2, 3]

    # Whisper не должен аварийно завершаться.
    assert not any(
        "Whisper" in str(call)
        or "распознавания" in str(call)
        for call in log.error.call_args_list
    )


def test_telegram_restart_start_failure(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    listener = Mock(name="telegram_listener")
    supervisor.telegram_listener = listener

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    class FailingThread(FakeThread):
        def start(self):
            raise RuntimeError("Telegram thread start failed")

    def thread_factory(target, daemon):
        # Первый повторный запуск Telegram завершится ошибкой.
        if target is listener and len(FakeThread.instances) == 2:
            return FailingThread(target, daemon)

        return FakeThread(target, daemon)

    supervisor.thread_factory = thread_factory

    ticks = iter([
        100.0,   # Обнаружена остановка Telegram
        105.0,   # Попытка 1: ошибка запуска
        106.0,   # Планирование следующей попытки
        120.9,   # Ещё рано
        121.0,   # Попытка 2: успешный запуск
        122.0,   # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 122.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        # Whisper работает постоянно.
        if thread is FakeThread.instances[0]:
            return thread.started

        # Исходный Telegram завершился.
        # Новый поток после второй попытки работает.
        return (
            len(FakeThread.instances) >= 4
            and thread is FakeThread.instances[3]
            and thread.started
        )

    with patch.object(FakeThread, "is_alive", fake_is_alive):
        supervisor.run()

    # Whisper + исходный Telegram + неудачный + успешный.
    assert len(FakeThread.instances) == 4

    assert FakeThread.instances[0].started
    assert FakeThread.instances[1].started
    assert not FakeThread.instances[2].started
    assert FakeThread.instances[3].started

    # Проверяем регистрацию ошибки.
    log.exception.assert_called_once_with(
        "Ошибка перезапуска Telegram"
    )

    log.error.assert_called_once_with(
        "Поток Telegram неожиданно завершился"
    )

    # Вторая попытка успешно запустила Telegram.
    assert any(
        call.args == (
            "Поток Telegram перезапущен, попытка %d",
            2,
        )
        for call in log.info.call_args_list
    )

    # Whisper не перезапускался.
    assert FakeThread.instances[0].target is supervisor.transcriber

    log.critical.assert_not_called()


def test_telegram_restart_counter_resets_after_30_minutes(monkeypatch):
    from unittest.mock import patch
    import core.restart_policy as restart_module

    FakeThread.instances = []

    supervisor, _, log = make_supervisor(queue.Queue())

    listener = Mock(name="telegram_listener")
    supervisor.telegram_listener = listener

    now = [100.0]

    monkeypatch.setattr(
        restart_module.time,
        "monotonic",
        lambda: now[0],
    )

    ticks = iter([
        105.0,     # Первый перезапуск Telegram
        1904.9,    # Почти 30 минут работы
        1905.0,    # Ровно 30 минут
        2000.0,    # Вторая авария Telegram
        2005.0,    # Повторный запуск через 5 секунд
        2006.0,    # Завершение теста
    ])

    def fake_get(timeout=None):
        now[0] = next(ticks)

        if now[0] == 2006.0:
            raise KeyboardInterrupt

        raise queue.Empty

    supervisor.frames.get = Mock(side_effect=fake_get)

    def fake_is_alive(thread):
        index = FakeThread.instances.index(thread)

        if index == 0:
            return thread.started  # Whisper работает

        if index == 1:
            return False  # Первый Telegram завершился

        if index == 2:
            return thread.started and now[0] < 2000.0

        return thread.started

    with patch.object(FakeThread, "is_alive", fake_is_alive):
        supervisor.run()

    # Whisper + исходный Telegram + два перезапуска.
    assert len(FakeThread.instances) == 4

    restart_logs = [
        call
        for call in log.info.call_args_list
        if call.args
        and call.args[0] == "Поток Telegram перезапущен, попытка %d"
    ]

    # После 30 минут счётчик начинается с первой попытки.
    assert [call.args[1] for call in restart_logs] == [1, 1]

    log.critical.assert_not_called()
