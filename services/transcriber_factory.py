from core.recognition import WhisperRecognizer
from core.correction import build_llm_corrector
from core.pipeline import build_text_pipeline
from core.event_processor import EventProcessor
from core.transcriber import Transcriber
from core.analyzer import correction_is_safe
from core.notification_templates import load_templates
from core.notification_spectral import (
    DEFAULT_MAX_EXTRA, DEFAULT_MAX_OUTSIDE_S, DEFAULT_THRESHOLD, SpectralNotificationMatcher,
)

from services.event_store import save_event
from services.word_rule_engine import apply_verified_word_rules


def build_transcriber(
    config,
    segments,
    callsigns,
    commands,
    watch,
    prompt,
    telegram_service,
    cooldown,
    log,
    pause_gate=None,
    health=None,
    recorder=None,
):
    """Создаёт компоненты распознавания без запуска рабочего цикла."""

    # Фрази-галюцинації, короткий шум і алиаси — з SQLite, оновлюються без перезапуску.
    from services.text_lists import LiveTextLists, compose_prompt
    from services.dictionary_loader import LiveDictionaries
    from services.runtime_settings import LiveRuntimeSettings
    text_lists = LiveTextLists(config.DB_PATH, log)
    text_lists.refresh(force=True)

    # Словники теж живі: усі споживачі отримують об’єкти, що дивляться на поточний знімок.
    live_dictionaries = LiveDictionaries(config.DB_PATH, callsigns, commands, watch, prompt, log)
    live_dictionaries.refresh(force=True)
    callsigns, commands, watch = live_dictionaries.dictionaries

    correct_with_llm = build_llm_corrector(
        config.USE_LLM,
        config.LLM_REPO,
        callsigns,
        commands,
        log,
    )

    dictionaries = (callsigns, commands, watch)

    def apply_word_corrections(text):
        revised = apply_verified_word_rules(
            text,
            config.DB_PATH,
            dictionaries,
        )
        return revised, revised != text

    process_text = build_text_pipeline(
        correct_with_llm=correct_with_llm,
        apply_learned_corrections=apply_word_corrections,
        dictionaries=dictionaries,
        correction_is_safe=correction_is_safe,
        log=log,
    )

    event_processor = EventProcessor(
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        short_garbage=text_lists.short_garbage_view(),
        save_event=save_event,
        telegram_service=telegram_service,
        fresh=cooldown.fresh,
        select_recipients=telegram_service.recipients_for,
        rollback=cooldown.rollback,
        db_path=config.DB_PATH,
        log_callsigns_only=config.LOG_CALLSIGNS_ONLY,
        log=log,
    )

    def current_prompt():
        # Фрази зі списку «Підказка Whisper» + назви словників.
        return compose_prompt(text_lists.whisper_prompt, live_dictionaries.prompt)

    recognizer = WhisperRecognizer(
        model_repo=config.MODEL_REPO,
        language=config.LANGUAGE,
        sample_rate=config.SR,
        prompt=current_prompt(),
        log=log,
    )

    log.info("Підказка Whisper: фраз зі списку — %d", len(text_lists.whisper_prompt))

    def update_prompt(_value=None):
        prompt = current_prompt()
        if prompt != recognizer.prompt:
            recognizer.prompt = prompt
            log.info("Підказку Whisper оновлено: фраз зі списку — %d", len(text_lists.whisper_prompt))

    live_dictionaries.on_prompt(update_prompt)
    text_lists.on_change(update_prompt)

    spectral_matcher = None
    spectral_mode = getattr(config, "SPECTRAL_NOTIFICATION_MODE", "off")
    if spectral_mode not in ("off", "shadow", "active"):
        log.warning("Невідомий режим спектрального фільтра; використано спостереження")
        spectral_mode = "shadow"
    if spectral_mode != "off":
        templates = load_templates(config.NOTIFICATION_SOUNDS_DIR, config.SR, log)
        log.info("Фільтр звуків сповіщень: завантажено зразків — %d", len(templates))
        if spectral_mode != "off":
            spectral_matcher = SpectralNotificationMatcher(
                templates, config.SR, prepared=True,
                threshold=getattr(config, "SPECTRAL_NOTIFICATION_THRESHOLD", DEFAULT_THRESHOLD),
                max_outside_s=getattr(config, "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S", DEFAULT_MAX_OUTSIDE_S),
                max_extra=getattr(config, "SPECTRAL_NOTIFICATION_MAX_EXTRA", DEFAULT_MAX_EXTRA),
            )

    worker = Transcriber(
        segments=segments,
        recognizer=recognizer,
        process_text=process_text,
        event_processor=event_processor,
        sample_rate=config.SR,
        min_rms=config.MIN_RMS,
        normalize_audio=config.NORMALIZE,
        debug_rms=config.DEBUG_RMS,
        blacklist=(),
        text_lists=text_lists,
        log=log,
        pause_gate=pause_gate,
        health=health,
        recorder=recorder,
        spectral_matcher=spectral_matcher,
        spectral_mode=spectral_mode,
    )

    def apply_settings(values):
        # Застосовується в потоці розпізнавання перед наступним сегментом.
        if hasattr(cooldown, "cooldown_s"):
            cooldown.cooldown_s = values["COOLDOWN_S"]
        event_processor.log_callsigns_only = values["LOG_CALLSIGNS_ONLY"]
        worker.garbage_limits = {
            "min_words": values["GARBAGE_MIN_WORDS"],
            "min_unique_ratio": values["GARBAGE_MIN_UNIQUE_RATIO"],
            "max_words_per_s": values["GARBAGE_MAX_WORDS_PER_S"],
            "extra_words": values["GARBAGE_EXTRA_WORDS"],
        }

    worker.low_confidence = {
        "max_words": getattr(config, "LOW_CONFIDENCE_MAX_WORDS", 2),
        "logprob": getattr(config, "LOW_CONFIDENCE_LOGPROB", -0.7),
    }

    live_settings = LiveRuntimeSettings(config.DB_PATH, apply_settings, log)
    live_settings.refresh(force=True)
    worker.refreshers = (live_dictionaries.refresh, live_settings.refresh)
    worker.live_dictionaries = live_dictionaries
    worker.live_settings = live_settings
    return worker
