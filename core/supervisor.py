import queue
import signal
import time
from contextlib import ExitStack, contextmanager, nullcontext
import threading

import sounddevice as sd

from core.vad import VADSegmenter
from core.queue_monitor import QueueDropMonitor
from core.restart_policy import RestartPolicy


@contextmanager
def ignore_repeated_sigint():
    """Первый Ctrl+C останавливает работу, повторные не мешают закрытию."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous_handler = signal.getsignal(signal.SIGINT)
    interrupted = False

    def on_sigint(signum, frame):
        nonlocal interrupted

        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_sigint)

    try:
        yield
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

        def clear_queue(q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                else:
                    q.task_done()

        def transition(should_pause):
            nonlocal paused, segmenter, retry_at

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
                self.log.info("⏸ Приём приостановлен: микрофон закрыт")

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

                if self.pause_gate is not None:
                    self.pause_gate.set_paused(False)

                if self.runtime_state is not None:
                    self.runtime_state.set("listening")

                was_paused = paused is True
                paused = False

                if was_paused:
                    self.log.info(
                        "▶️ Приём возобновлён: микрофон открыт"
                    )

        with ignore_repeated_sigint(), self.heartbeat_context():
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
                while True:
                    if transcriber_thread.is_alive():
                        if (
                            restart_policy.attempts > 0
                            and restart_policy.clock() - transcriber_started_at >= 1800.0
                        ):
                            restart_policy.reset()
                            restart_exhausted_logged = False
                            self.log.info(
                                "Счётчик перезапусков Whisper сброшен после 30 минут работы"
                            )
                    else:
                        if not transcriber_error_logged:
                            self.log.error("Поток распознавания неожиданно завершился")
                            transcriber_error_logged = True

                        if restart_policy.should_restart():
                            try:
                                new_thread = self.thread_factory(
                                    target=self.transcriber,
                                    daemon=True,
                                )
                                new_thread.start()
                            except Exception:
                                self.log.exception("Ошибка перезапуска Whisper")
                            else:
                                transcriber_thread = new_thread
                                transcriber_started_at = restart_policy.clock()
                                transcriber_error_logged = False
                                self.log.info(
                                    "Поток Whisper перезапущен, попытка %d",
                                    restart_policy.attempts,
                                )

                        elif restart_policy.exhausted and not restart_exhausted_logged:
                            self.log.critical("Исчерпаны попытки перезапуска Whisper")
                            restart_exhausted_logged = True
                            raise RuntimeError("Исчерпаны попытки перезапуска Whisper")
                    if telegram_thread is not None:
                        if telegram_thread.is_alive():
                            if (
                                telegram_restart_policy.attempts > 0
                                and telegram_restart_policy.clock() - telegram_started_at >= 1800.0
                            ):
                                telegram_restart_policy.reset()
                                telegram_restart_exhausted_logged = False
                                self.log.info("Счётчик перезапусков Telegram сброшен после 30 минут работы")
                        else:
                            if not telegram_error_logged:
                                self.log.error("Поток Telegram неожиданно завершился")
                                telegram_error_logged = True

                            if telegram_restart_policy.should_restart():
                                try:
                                    new_telegram_thread = self.thread_factory(
                                        target=self.telegram_listener,
                                        daemon=True,
                                    )
                                    new_telegram_thread.start()
                                except Exception:
                                    self.log.exception("Ошибка перезапуска Telegram")
                                else:
                                    telegram_thread = new_telegram_thread
                                    telegram_started_at = telegram_restart_policy.clock()
                                    telegram_error_logged = False
                                    self.log.info(
                                        "Поток Telegram перезапущен, попытка %d",
                                        telegram_restart_policy.attempts,
                                    )

                            elif (
                                telegram_restart_policy.exhausted
                                and not telegram_restart_exhausted_logged
                            ):
                                self.log.critical("Исчерпаны попытки перезапуска Telegram")
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
                        monitor.check()
                        continue

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

            except KeyboardInterrupt:
                self.log.info("Остановлено пользователем.")
            finally:
                if self.pause_gate is not None:
                    self.pause_gate.set_paused(True)

                audio_stack.close()

                if self.runtime_state is not None:
                    self.runtime_state.set("stopped")
