import os
import sqlite3
import time

from shared.correction import build_llm_corrector
from shared.pipeline import build_text_pipeline
from server.event_processor import EventProcessor
from server.transcriber import Transcriber
from shared.analyzer import correction_is_safe
from shared.notification_templates import load_templates
from shared.notification_spectral import (
    SpectralNotificationMatcher,
)

from shared.event_store import save_event
from shared.word_rule_engine import apply_verified_word_rules
from shared.paths import place, root_of


def build_recognizer(config, prompt, log):
    """RADIO_ASR_URL — модель на вузлі Mac; порожньо — локальна MLX (як було)."""
    url = os.environ.get("RADIO_ASR_URL", "").strip()
    common = {"language": config.LANGUAGE, "sample_rate": config.SR, "prompt": prompt, "log": log}
    if url:
        from server.remote_recognizer import RemoteRecognizer
        return RemoteRecognizer(url, **common)
    from shared.recognition import WhisperRecognizer
    return WhisperRecognizer(model_repo=config.MODEL_REPO, **common)


def prompt_codes(db_path):
    """Останній код поточної доби для підказки Whisper (порожньо, якщо кодів немає).

    18.11.2026 на 475 записах із другим шансом: точних фраз 36 → 39, числа 26 → 30,5 %,
    петель 3 → 2, хибного тексту на шумі не додалося; p90 затримки +1,1 с.
    """
    from shared import daily_codes
    from shared.sqlite_connection import get_db
    with get_db(db_path) as db:
        return tuple(daily_codes.read_day(db, daily_codes.local_day())[-1:])


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
    health=None,
    recorder=None,
    abandon_event=None,
    radio_stream=None,
):
    """Создаёт компоненты распознавания без запуска рабочего цикла."""

    # Фрази-галюцинації, короткий шум і алиаси — з SQLite, оновлюються без перезапуску.
    from shared.text_lists import LiveTextLists, compose_prompt
    from shared.dictionary_loader import LiveDictionaries
    from shared.runtime_settings import LiveRuntimeSettings
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
        radio_stream=radio_stream,
    )

    # Вмикається в панелі («Обробка без перезапуску»); початкове значення — config.py.
    code_cache = {"at": None, "codes": (), "enabled": bool(config.WHISPER_PROMPT_DAILY_CODE)}

    def todays_code():
        """Останній код поточної доби кодів (змінюються рідко — читаємо не частіше за 30 с)."""
        if not code_cache["enabled"]:
            return ()
        now = time.monotonic()
        if code_cache["at"] is None or now - code_cache["at"] >= 30:
            code_cache["at"] = now
            try:
                code_cache["codes"] = prompt_codes(config.DB_PATH)
            except Exception:
                log.warning("Код доби для підказки Whisper не прочитано; лишається попередній")
        return code_cache["codes"]

    def current_prompt():
        # Фрази зі списку «Підказка Whisper» + останній код доби + назви словників.
        return compose_prompt((*text_lists.whisper_prompt, *todays_code()), live_dictionaries.prompt)

    recognizer = build_recognizer(config, current_prompt(), log)

    log.info("Підказка Whisper: фраз зі списку — %d", len(text_lists.whisper_prompt))

    def update_prompt(_value=None):
        prompt = current_prompt()
        if prompt != recognizer.prompt:
            recognizer.prompt = prompt
            log.info("Підказку Whisper оновлено: фраз зі списку — %d", len(text_lists.whisper_prompt))

    live_dictionaries.on_prompt(update_prompt)
    text_lists.on_change(update_prompt)

    # Фільтр створюється завжди: режим і пороги змінюються в панелі на льоту (apply_settings нижче).
    spectral_mode = config.SPECTRAL_NOTIFICATION_MODE
    templates = load_templates(config.NOTIFICATION_SOUNDS_DIR, config.SR, log)
    log.info("Фільтр звуків сповіщень: завантажено зразків — %d", len(templates))
    spectral_matcher = SpectralNotificationMatcher(
        templates, config.SR, prepared=True, threshold=config.SPECTRAL_NOTIFICATION_THRESHOLD,
        max_outside_s=config.SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S, max_extra=config.SPECTRAL_NOTIFICATION_MAX_EXTRA)

    worker = Transcriber(
        abandon_event=abandon_event,
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
        health=health,
        recorder=recorder,
        spectral_matcher=spectral_matcher,
        spectral_mode=spectral_mode,
    )
    if radio_stream is not None:
        # Той самий фільтр і режим — для каналу «наживо без сигналів».
        radio_stream.set_signal_matcher(spectral_matcher, lambda: worker.spectral_mode)

    code_format_cache = {}

    def format_confirmed_code_commas(text, recorded_at):
        from shared import daily_codes
        from shared.sqlite_connection import get_db

        day = daily_codes.local_day(recorded_at)
        now = time.monotonic()
        cached = code_format_cache.get(day)
        if cached is None or now - cached[0] >= 30:
            try:
                with get_db(config.DB_PATH) as db:
                    codes = tuple(daily_codes.read(db, day))
            except (sqlite3.Error, OSError, ValueError) as exc:
                log.warning("Коди доби для форматування не прочитано: %s", type(exc).__name__)
                return text
            code_format_cache[day] = (now, codes)
            if len(code_format_cache) > 14:
                oldest = min(code_format_cache, key=lambda value: code_format_cache[value][0])
                del code_format_cache[oldest]
        else:
            codes = cached[1]
        return daily_codes.remove_confirmed_code_commas(text, codes, dictionaries)

    worker.daily_code_formatter = format_confirmed_code_commas

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
        worker.low_confidence = {"max_words": values["LOW_CONFIDENCE_MAX_WORDS"], "logprob": values["LOW_CONFIDENCE_LOGPROB"]}
        worker.min_rms = values["MIN_RMS"]
        worker.retry_without_prompt = values["WHISPER_RETRY_WITHOUT_PROMPT"]
        worker.spectral_mode = values["SPECTRAL_NOTIFICATION_MODE"]
        spectral_matcher.threshold = values["SPECTRAL_NOTIFICATION_THRESHOLD"]
        spectral_matcher.max_outside_s = values["SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S"]
        spectral_matcher.max_extra = values["SPECTRAL_NOTIFICATION_MAX_EXTRA"]
        if code_cache["enabled"] != values["WHISPER_PROMPT_DAILY_CODE"]:
            code_cache["enabled"], code_cache["at"] = values["WHISPER_PROMPT_DAILY_CODE"], None
            update_prompt()

    live_settings = LiveRuntimeSettings(config.DB_PATH, apply_settings, log)
    live_settings.refresh(force=True)
    from pathlib import Path
    from shared.dictionary_number_evidence import load_rules
    def refresh_number_rules():
        archive=getattr(config,'RECORDINGS_DIR',place(root_of(config.DB_PATH), ".local-recordings"))
        callsigns.number_rules=load_rules(archive,callsigns,commands)
        from shared.diagnostics import runtime
        runtime.gauge('dictionary.number_rules',len(callsigns.number_rules))
    refresh_number_rules()
    # update_prompt — і для коду доби: новий код чи зміна доби о 20:00 потрапляють у підказку самі.
    worker.refreshers = (live_dictionaries.refresh, live_settings.refresh, update_prompt, refresh_number_rules)
    worker.live_dictionaries = live_dictionaries
    worker.live_settings = live_settings
    if recorder is not None:
        worker.deferred = recorder.store
    return worker
