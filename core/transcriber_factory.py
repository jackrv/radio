from core.recognition import WhisperRecognizer
from core.correction import build_llm_corrector
from core.pipeline import build_text_pipeline
from core.event_processor import EventProcessor
from core.transcriber import Transcriber
from core.analyzer import correction_is_safe

from services.event_store import save_event
from core.word_rule_engine import apply_verified_word_rules


def build_transcriber(
    config,
    segments,
    callsigns,
    commands,
    watch,
    prompt,
    telegram_service,
    cooldown,
    bot_state,
    log,
):
    """Создаёт компоненты распознавания без запуска рабочего цикла."""

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
        short_garbage=config.SHORT_GARBAGE,
        save_event=save_event,
        telegram_service=telegram_service,
        fresh=cooldown.fresh,
        rollback=cooldown.rollback,
        state_lock=bot_state.lock,
        get_notify_mode=bot_state.get_notify_mode,
        db_path=config.DB_PATH,
        log_callsigns_only=config.LOG_CALLSIGNS_ONLY,
        log=log,
    )

    recognizer = WhisperRecognizer(
        model_repo=config.MODEL_REPO,
        language=config.LANGUAGE,
        sample_rate=config.SR,
        prompt=prompt,
        log=log,
    )

    return Transcriber(
        segments=segments,
        recognizer=recognizer,
        process_text=process_text,
        event_processor=event_processor,
        sample_rate=config.SR,
        min_rms=config.MIN_RMS,
        normalize_audio=config.NORMALIZE,
        debug_rms=config.DEBUG_RMS,
        blacklist=config.BLACKLIST,
        log=log,
    )
