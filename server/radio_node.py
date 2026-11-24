"""Радіовузол без розпізнавання: захоплення → VAD → потоки для iPhone + PTT.

Те, що переїде на Raspberry Pi 5 поруч із рацією: не потрібні Mac, Whisper,
база подій і Telegram. Працюють «Наживо», «Наживо без сигналів» (фільтр сигналів —
numpy), передача з iPhone на вихід звуку з ключем PTT і напівдуплекс.
«Відібрані» й тексти приходять файлами від server.asr_service (phrase_feed); без неї канал порожній.

Розпізнавання підключається через ``segment_sink`` (готовий VAD-сегмент PCM16):
зараз сегменти відкидаються; далі — надсилання на Mac (черга з підтвердженням)
або локальна модель. Див. docs/ideas.md «Радіовузол на RPi5».

Запуск: ``.venv/bin/python -m server.radio_node``. Не запускайте поруч із
другим вузлом на тому самому пристрої: обидва захоплюють мікрофон і займають порт потоку.
"""

import logging
import os
import queue
import threading
from shared.paths import place


def build_node(config, log, *, segment_sink=None, stream_starter=None, supervisor_cls=None, control=None):
    """Збирає вузол; повертає (supervisor, stream) або (None, None), якщо потік не стартував."""
    from shared.audio import apply_gain
    from server.bounded_queue import DropOldestQueue
    from server.segment_timing import capture_frame
    from shared.audio_devices import configured, input_stream_factory
    from shared.radio_stream import start_radio_stream
    from server.level_meter import LevelMeter

    if supervisor_cls is None:
        from server.supervisor import ApplicationSupervisor as supervisor_cls

    # Для вузла потік — головна функція: вмикається незалежно від RADIO_STREAM.
    stream = (stream_starter or start_radio_stream)(config, log, force=True)
    if stream is None:
        return None, None

    _attach_signal_filter(stream, config, log)

    stream.level = meter = LevelMeter()
    frames = DropOldestQueue(100)
    segments = DropOldestQueue(5)
    stop = threading.Event()

    def on_audio(indata, frame_count, time_info, status):
        if status:
            log.warning("Стан аудіопотоку: %s", status)
        frame = capture_frame(apply_gain(indata, config.GAIN), frame_count, config.SR, time_info)
        frames.put(frame)
        meter.add(frame)

    def consume_segments():
        """Замість Whisper: передати сегмент далі (або відкинути, поки приймача тексту немає)."""
        while not stop.is_set():
            try:
                item = segments.get(timeout=0.5)
            except queue.Empty:
                continue
            meter.segment(item)
            if segment_sink is not None:
                try:
                    segment_sink(item)
                except Exception:
                    log.exception("Радіовузол: помилка передачі сегмента на розпізнавання")

    supervisor = supervisor_cls(
        frames=frames,
        segments=segments,
        on_audio=on_audio,
        transcriber=consume_segments,
        telegram_listener=None,
        sample_rate=config.SR,
        frame_ms=config.FRAME_MS,
        frame_len=config.FRAME_LEN,
        vad_level=config.VAD_LEVEL,
        preroll_ms=config.PREROLL_MS,
        silence_end_ms=config.SILENCE_END_MS,
        min_speech_ms=config.MIN_SPEECH_MS,
        max_segment_s=config.MAX_SEGMENT_S,
        log=log,
        stream_factory=input_stream_factory(configured("RADIO_INPUT_DEVICE", config), log),
        on_live_audio=stream.live_audio,
        input_muted=stream.input_muted,
        transcriber_stop=stop.set,
        transcriber_abandon=stop.set,
        **({} if control is None else {"pause_checker": control.request, "runtime_state": control,
                                       "on_suspension": control.record_gap,
                                       "heartbeat_context": lambda: control.heartbeat(log=log)}),
    )
    return supervisor, stream


def _attach_signal_filter(stream, config, log):
    try:
        from shared.notification_spectral import SpectralNotificationMatcher
        from shared.notification_templates import load_templates
        templates = load_templates(config.NOTIFICATION_SOUNDS_DIR, config.SR, log)
        matcher = SpectralNotificationMatcher(
            templates, config.SR, prepared=True, threshold=config.SPECTRAL_NOTIFICATION_THRESHOLD,
            max_outside_s=config.SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S,
            max_extra=config.SPECTRAL_NOTIFICATION_MAX_EXTRA)
        mode = config.SPECTRAL_NOTIFICATION_MODE
        stream.set_signal_matcher(matcher, lambda: mode)
        log.info("Радіовузол: зразків сигналів — %d (режим %s)", len(templates), mode)
    except Exception:
        log.exception("Радіовузол: фільтр сигналів недоступний; «наживо без сигналів» = «наживо»")


def main():
    from dotenv import load_dotenv

    import config
    from server.log_security import configure_logging

    load_dotenv(config.BASE_DIR / ".env")
    configure_logging("", starting_message="Радіовузол без розпізнавання; чекаємо аудіовхід (USB-карта).")
    log = logging.getLogger("radio.node")
    sink = None
    if os.environ.get("RADIO_SPOOL", "").strip() in ("1", "true", "yes", "on"):
        from server.segment_spool import SegmentSpool
        spool = SegmentSpool(os.environ.get("RADIO_SPOOL_DIR", "").strip() or place(config.BASE_DIR, ".local-segment-spool"))
        sink = lambda item: spool.put(item, config.SR)
    from server.node_control import NodeControl, directory
    from server.phrase_feed import PhraseInbox, feed_dir
    hold = os.environ.get("RADIO_LISTENING_HOLD", "1").strip().lower() not in ("0", "false", "no", "off")
    control = NodeControl(directory(config), listening_hold=hold)
    control.apply_startup(config)
    from shared import audio_choice
    audio_choice.apply(config.BASE_DIR, config)
    supervisor, stream = build_node(config, log, segment_sink=sink, control=control)
    if supervisor is None:
        log.error("Радіовузол не запущено: потік недоступний (Tailscale? порт зайнятий?)",
                  extra={"console": True})
        raise SystemExit(1)
    texts_stop = threading.Event()
    texts = PhraseInbox(feed_dir(directory(config)), stream, log).start(texts_stop)
    log.info("Радіовузол працює без розпізнавання (Ctrl+C — вихід)", extra={"console": True})
    try:
        supervisor.run()
    finally:
        texts_stop.set()
        texts.join(2)
        stream.stop()


if __name__ == "__main__":
    main()
