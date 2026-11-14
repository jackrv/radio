import queue
import signal
import time
from contextlib import ExitStack, contextmanager, nullcontext
import threading

import sounddevice as sd

from core.vad import VADSegmenter
from core.queue_monitor import QueueDropMonitor
from core.restart_policy import RestartPolicy
from core.diagnostics import runtime


@contextmanager
def ignore_repeated_sigint():
    """Первый Ctrl+C — штатная остановка, второй — прерывание."""

    stop_requested = threading.Event()

    if threading.current_thread() is not threading.main_thread():
        yield stop_requested
        return

    previous_handler = signal.getsignal(signal.SIGINT)
    interrupt_count = 0

    def on_sigint(signum, frame):
        nonlocal interrupt_count
        interrupt_count += 1

        if interrupt_count == 1:
            stop_requested.set()
        else:
            signal.signal(signal.SIGINT, previous_handler)
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_sigint)

    try:
        yield stop_requested
    finally:
        signal.signal(signal.SIGINT, previous_handler)


class ApplicationSupervisor:
    """Управляет запуском потоков и циклом захвата аудио."""

    def __init__(
        self,
        frames,
        segments,
        on_audio,
        transcriber,
        telegram_listener,
        sample_rate,
        frame_ms,
        frame_len,
        vad_level,
        preroll_ms,
        silence_end_ms,
        min_speech_ms,
        max_segment_s,
        log,
        stream_factory=None,
        segmenter_factory=None,
        thread_factory=None,
        heartbeat_context=None,
        pause_checker=None,
        pause_gate=None,
        runtime_state=None,
        stall_timeout_s=10.0,
        suspend_threshold_s=60.0,
        wall_clock=None,
        on_suspension=None,
    ):
        self.frames = frames
        self.segments = segments
        self.on_audio = on_audio
        self.transcriber = transcriber
        self.telegram_listener = telegram_listener

        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_len = frame_len
        self.vad_level = vad_level
        self.preroll_ms = preroll_ms
        self.silence_end_ms = silence_end_ms
        self.min_speech_ms = min_speech_ms
        self.max_segment_s = max_segment_s

        self.log = log
        self.stream_factory = stream_factory or sd.RawInputStream
        self.segmenter_factory = segmenter_factory or VADSegmenter
        self.thread_factory = thread_factory or threading.Thread
        self.heartbeat_context = heartbeat_context or nullcontext
        self.pause_checker = pause_checker
        self.pause_gate = pause_gate
        self.runtime_state = runtime_state
        # Відкритий потік без кадрів довше цього часу вважається зависшим.
        self.stall_timeout_s = stall_timeout_s
        # Розрив між ітераціями циклу довше цього — сон Mac або зупинка процесу.
        self.suspend_threshold_s = suspend_threshold_s
        self.wall_clock = wall_clock or time.time
        # Сповіщення про розрив приймання (не повинне блокувати цикл аудіо).
        self.on_suspension = on_suspension

    def run(self):
        """Запускает рабочие потоки и цикл обработки аудиокадров."""

        transcriber_thread = self.thread_factory(
            target=self.transcriber,
            daemon=True,
        )
        transcriber_thread.start()

        telegram_thread = None
        if self.telegram_listener is not None:
            telegram_thread = self.thread_factory(
                target=self.telegram_listener,
                daemon=True,
            )
            telegram_thread.start()

        def new_segmenter():
            return self.segmenter_factory(
                sample_rate=self.sample_rate,
                frame_ms=self.frame_ms,
                vad_level=self.vad_level,
                preroll_ms=self.preroll_ms,
                silence_end_ms=self.silence_end_ms,
                min_speech_ms=self.min_speech_ms,
                max_segment_s=self.max_segment_s,
            )

        segmenter = new_segmenter()

        monitor = QueueDropMonitor(
            self.frames, self.segments, self.log
        )

        audio_stack = ExitStack()
        paused = None
        retry_at = 0.0
        last_frame_at = time.monotonic()

        def clear_queue(q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                else:
                    q.task_done()

        def restart_stalled_stream():
            """Закриває потік без кадрів; наступна ітерація відкриє його штатно."""
            nonlocal paused, retry_at, last_frame_at

            runtime.count("audio.stream_stalls")
            self.log.error(
                "Мікрофон не надсилає звук %d с; перевідкриваю аудіопотік",
                round(self.stall_timeout_s),
            )
            audio_stack.close()
            if self.pause_gate is not None:
                self.pause_gate.set_paused(True)
            if self.runtime_state is not None:
                self.runtime_state.set("stopped")
            paused = True
            retry_at = 0.0
            last_frame_at = time.monotonic()

        def transition(should_pause):
            nonlocal paused, segmenter, retry_at, last_frame_at

            if paused is should_pause:
                return

            if should_pause:
                if self.pause_gate is not None:
                    self.pause_gate.set_paused(True)

                # Сначала закрываем аудиопоток,
                # затем удаляем накопившиеся данные.
                audio_stack.close()
                clear_queue(self.frames)
                clear_queue(self.segments)

                if self.runtime_state is not None:
                    self.runtime_state.set("paused")

                paused = True
                self.log.info("⏸ Приймання призупинено: мікрофон закрито")

            else:
                # После ошибки открытия не повторяем попытку
                # чаще одного раза в пять секунд.
                if time.monotonic() < retry_at:
                    return

                # При первом запуске очереди не очищаем.
                # Очистка нужна только при выходе из паузы.
                if paused is True:
                    clear_queue(self.frames)
                    clear_queue(self.segments)

                # Новый VAD не содержит старой незавершённой фразы.
                segmenter = new_segmenter()

                try:
                    audio_stack.enter_context(self.stream_factory(
                        samplerate=self.sample_rate,
                        blocksize=self.frame_len,
                        dtype="int16",
                        channels=1,
                        callback=self.on_audio,
                    ))
                except Exception:
                    # Не объявляем микрофон работающим.
                    paused = True
                    retry_at = time.monotonic() + 5.0

                    if self.pause_gate is not None:
                        self.pause_gate.set_paused(True)

                    if self.runtime_state is not None:
                        self.runtime_state.set("stopped")

                    self.log.exception(
                        "Не удалось открыть микрофон. "
                        "Повторная попытка через 5 секунд"
                    )
                    return

                retry_at = 0.0
                last_frame_at = time.monotonic()

                if self.pause_gate is not None:
                    self.pause_gate.set_paused(False)

                if self.runtime_state is not None:
                    self.runtime_state.set("listening")

                was_paused = paused is True
                paused = False

                if was_paused:
                    self.log.info(
                        "▶️ Приймання відновлено: мікрофон відкрито"
                    )

        with ignore_repeated_sigint() as stop_requested, self.heartbeat_context():
            try:
                requested_pause = (
                    bool(self.pause_checker())
                    if self.pause_checker is not None
                    else False
                )
                transition(requested_pause)

                transcriber_error_logged = False
                restart_policy = RestartPolicy()
                transcriber_started_at = restart_policy.clock()
                restart_exhausted_logged = False
                telegram_error_logged = False
                telegram_restart_policy = RestartPolicy()
                telegram_started_at = (
                    telegram_restart_policy.clock()
                    if telegram_thread is not None else None
                )
                telegram_restart_exhausted_logged = False
                last_wall = self.wall_clock()
                last_mono = time.monotonic()
                while not stop_requested.is_set():
                    # Сон Mac (наприклад, закрита кришка) зупиняє процес повністю.
                    # Беремо більший із розривів: monotonic на macOS може не йти уві сні.
                    now_wall, now_mono = self.wall_clock(), time.monotonic()
                    gap = max(now_wall - last_wall, now_mono - last_mono)
                    last_wall, last_mono = now_wall, now_mono
                    if gap >= self.suspend_threshold_s:
                        runtime.count("receiver.suspensions")
                        runtime.observe("receiver.suspended", gap)
                        self.log.warning(
                            "Приймання не працювало ~%d хв (сон Mac або зупинка процесу); "
                            "незавершену фразу скинуто",
                            max(1, round(gap / 60)),
                        )
                        segmenter = new_segmenter()
                        last_frame_at = now_mono
                        if self.on_suspension is not None:
                            try:
                                self.on_suspension(gap)
                            except Exception:
                                self.log.exception("Не вдалося повідомити про розрив приймання")
                    if transcriber_thread.is_alive():
                        runtime.gauge("workers.transcriber_alive", True)
                        if (
                            restart_policy.attempts > 0
                            and restart_policy.clock() - transcriber_started_at >= 1800.0
                        ):
                            restart_policy.reset()
                            restart_exhausted_logged = False
                            self.log.info(
                                "Лічильник перезапусків Whisper скинуто після 30 хвилин роботи"
                            )
                    else:
                        runtime.gauge("workers.transcriber_alive", False)
                        if not transcriber_error_logged:
                            self.log.error("Потік розпізнавання несподівано завершився")
                            transcriber_error_logged = True

                        if restart_policy.should_restart():
                            try:
                                new_thread = self.thread_factory(
                                    target=self.transcriber,
                                    daemon=True,
                                )
                                new_thread.start()
                            except Exception:
                                self.log.exception("Помилка перезапуску Whisper")
                            else:
                                transcriber_thread = new_thread
                                runtime.count("workers.transcriber_restarted")
                                transcriber_started_at = restart_policy.clock()
                                transcriber_error_logged = False
                                self.log.info(
                                    "Потік Whisper перезапущено, спроба %d",
                                    restart_policy.attempts,
                                )

                        elif restart_policy.exhausted and not restart_exhausted_logged:
                            self.log.critical("Вичерпано спроби перезапуску Whisper")
                            restart_exhausted_logged = True
                            raise RuntimeError("Вичерпано спроби перезапуску Whisper")
                    if telegram_thread is not None:
                        if telegram_thread.is_alive():
                            runtime.gauge("workers.telegram_alive", True)
                            if (
                                telegram_restart_policy.attempts > 0
                                and telegram_restart_policy.clock() - telegram_started_at >= 1800.0
                            ):
                                telegram_restart_policy.reset()
                                telegram_restart_exhausted_logged = False
                                self.log.info("Лічильник перезапусків Telegram скинуто після 30 хвилин роботи")
                        else:
                            runtime.gauge("workers.telegram_alive", False)
                            if not telegram_error_logged:
                                self.log.error("Потік Telegram несподівано завершився")
                                telegram_error_logged = True

                            if telegram_restart_policy.should_restart():
                                try:
                                    new_telegram_thread = self.thread_factory(
                                        target=self.telegram_listener,
                                        daemon=True,
                                    )
                                    new_telegram_thread.start()
                                except Exception:
                                    self.log.exception("Помилка перезапуску Telegram")
                                else:
                                    telegram_thread = new_telegram_thread
                                    runtime.count("workers.telegram_restarted")
                                    telegram_started_at = telegram_restart_policy.clock()
                                    telegram_error_logged = False
                                    self.log.info(
                                        "Потік Telegram перезапущено, спроба %d",
                                        telegram_restart_policy.attempts,
                                    )

                            elif (
                                telegram_restart_policy.exhausted
                                and not telegram_restart_exhausted_logged
                            ):
                                self.log.critical("Вичерпано спроби перезапуску Telegram")
                                telegram_restart_exhausted_logged = True
                    requested_pause = (
                        bool(self.pause_checker())
                        if self.pause_checker is not None
                        else False
                    )

                    transition(requested_pause)

                    if paused:
                        time.sleep(0.2)
                        continue

                    try:
                        frame = self.frames.get(timeout=1.0)
                    except queue.Empty:
                        if time.monotonic() - last_frame_at >= self.stall_timeout_s:
                            restart_stalled_stream()
                        monitor.check()
                        continue

                    last_frame_at = time.monotonic()

                    segment = segmenter.process(frame)

                    if segment is not None:
                        if self.pause_gate is None:
                            self.segments.put(segment)
                        else:
                            generation = self.pause_gate.tag()
                            if generation is not None:
                                self.segments.put(
                                    (generation, segment)
                                )

                    monitor.check()

                if stop_requested.is_set():
                    self.log.info("Зупинено користувачем.")

            except KeyboardInterrupt:
                self.log.info("Зупинено користувачем.")
            finally:
                if self.pause_gate is not None:
                    self.pause_gate.set_paused(True)

                audio_stack.close()

                if self.runtime_state is not None:
                    self.runtime_state.set("stopped")
