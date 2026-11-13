import queue
import signal
from contextlib import contextmanager
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

        segmenter = self.segmenter_factory(
            sample_rate=self.sample_rate,
            frame_ms=self.frame_ms,
            vad_level=self.vad_level,
            preroll_ms=self.preroll_ms,
            silence_end_ms=self.silence_end_ms,
            min_speech_ms=self.min_speech_ms,
            max_segment_s=self.max_segment_s,
        )

        monitor = QueueDropMonitor(
            self.frames, self.segments, self.log
        )

        with ignore_repeated_sigint(), self.stream_factory(
            samplerate=self.sample_rate,
            blocksize=self.frame_len,
            dtype="int16",
            channels=1,
            callback=self.on_audio,
        ):
            try:
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
                    try:
                        frame = self.frames.get(timeout=1.0)
                    except queue.Empty:
                        monitor.check()
                        continue

                    segment = segmenter.process(frame)

                    if segment is not None:
                        self.segments.put(segment)

                    monitor.check()

            except KeyboardInterrupt:
                self.log.info("Остановлено пользователем.")
