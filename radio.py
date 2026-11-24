import os
import sys

# Автоматичний перезапуск через .venv проєкту, якщо запущено іншим Python.
from shared.project_python import select_python

venv_python = select_python()
if os.path.exists(venv_python) and os.path.abspath(sys.executable) != os.path.abspath(venv_python):
    os.execve(venv_python, [venv_python] + sys.argv, os.environ)

import logging
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from core.log_security import configure_logging
from shared.diagnostics import runtime as diagnostics
import shutil

from services.transcriber_factory import build_transcriber

from shared.audio import apply_gain
from core.segment_timing import capture_frame
from core.supervisor import ApplicationSupervisor
from core.pause_gate import PauseGate
from core.cooldown import Cooldown
from core.bounded_queue import DropOldestQueue
from services.receiver_status import (
    receiver_heartbeat,
    ReceiverRuntimeState,
)
from shared.receiver_control import reception_request
from services.management.settings import apply_startup_settings
from dotenv import load_dotenv




from shared.event_store import init_db as init_event_db

from services.telegram import TelegramService
from shared.dictionary_loader import load_dictionaries as load_sqlite_dictionaries
from services.dictionary_setup import ensure_dictionaries, interactive_console
from shared.user_notify_store import list_recipients
from shared.db_backup import start_backup_scheduler
from shared.model_exclusion import acquire_receiver_slot
from shared.instance_lock import OWNER_LOCK, acquire_instance_lock
from core.recognition_health import RecognitionHealth

import config

from config import (
    DB_PATH,
    COOLDOWN_S,
    GAIN,
    SR,
    FRAME_MS,
    FRAME_LEN,
    PREROLL_MS,
)


# Загружаем переменные из файла .env (если он существует в папке со скриптом)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------- Логирование ----------
configure_logging(
    os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
)
log = logging.getLogger("radio")

# ---------- Telegram ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# ---------- Настройки импортированы из config.py ----------

callsigns = None
commands = None
watch = None
PROMPT = ""

_instance_lock = None
_owner_lock = None
recognition_health = RecognitionHealth()
recording_archive = None
frames = DropOldestQueue(100)
segments = DropOldestQueue(5)
cooldown = Cooldown(COOLDOWN_S)

pause_gate = PauseGate()
receiver_runtime = ReceiverRuntimeState()

# Отримувачів і права визначає база користувачів; TELEGRAM_CHAT_ID потрібен
# лише для реєстрації першого адміністратора.
telegram_service = TelegramService(
    bot_token=BOT_TOKEN,
    sample_rate=SR,
    db_path=DB_PATH,
    log=log,
)

# ---------- База данных (потокобезопасно) ----------
# ---------- Утилиты ----------
def on_audio(indata, frame_count, time_info, status):
    if status:
        diagnostics.count("audio.stream_status")
        log.warning("Стан аудіопотоку: %s", status)
    frames.put(capture_frame(apply_gain(indata, GAIN), frame_count, SR, time_info))




# Потік відібраних фраз для iPhone; запускається в main(), якщо ввімкнено.
radio_stream = None


# ---------- Поток распознавания и LLM ----------
asr_stop = threading.Event()
asr_abandon = threading.Event()


def transcriber():
    worker = build_transcriber(
        config=config,
        segments=segments,
        callsigns=callsigns,
        commands=commands,
        watch=watch,
        prompt=PROMPT,
        telegram_service=telegram_service,
        cooldown=cooldown,
        log=log,
        pause_gate=pause_gate,
        health=recognition_health,
        recorder=recording_archive,
        stop_event=asr_stop,
        abandon_event=asr_abandon,
        radio_stream=radio_stream,
    )

    worker.recognizer.warmup()

    if not BOT_TOKEN:
        log.warning("Telegram вимкнено: не задано TELEGRAM_BOT_TOKEN у .env")

    log.info("Слухаю ефір... (Ctrl+C для виходу)", extra={"console": True})
    worker.run()


def load_dictionaries():
    global callsigns, commands, watch, PROMPT

    callsigns, commands, watch, PROMPT = load_sqlite_dictionaries(
        DB_PATH, log
    )


def main():
    from shared.model_exclusion import receiver_model_available
    if not receiver_model_available(config.BASE_DIR):
        raise SystemExit('Лабораторія використовує модель. Дочекайтеся завершення тесту перед запуском приймача.')
    # До створення таблиць: порожні словники пропонуємо імпортувати з dictionaries.txt.
    if not ensure_dictionaries(
        DB_PATH, config.DICTIONARIES_PATH, sys.executable,
        interactive=interactive_console(),
    ):
        raise SystemExit(2)
    # Два приймачі на одному мікрофоні подвоюють події та повідомлення.
    # Блокування тримається до кінця процесу й знімається ОС навіть після аварії.
    global _instance_lock
    try:
        _instance_lock = acquire_receiver_slot(config.BASE_DIR, DB_PATH)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    if _instance_lock is None:
        log.error("Інший приймач Radio 345 уже працює. Зупиніть його й запустіть знову.")
        raise SystemExit(2)
    global _owner_lock
    _owner_lock = acquire_instance_lock(config.BASE_DIR / OWNER_LOCK)
    if _owner_lock is None:
        log.error("Службу розпізнавання (services.asr_service) уже запущено з цією базою. Зупиніть її.")
        raise SystemExit(2)
    diagnostics.configure(
        config.BASE_DIR / "diagnostics.json",
        retention_days=config.DIAGNOSTICS_HISTORY_DAYS,
        max_sessions=config.DIAGNOSTICS_HISTORY_MAX_SESSIONS,
        max_history_bytes=config.DIAGNOSTICS_HISTORY_MAX_BYTES,
    )
    init_event_db(DB_PATH)
    apply_startup_settings(DB_PATH, config)
    from services.management.audio_settings import apply_audio_settings
    apply_audio_settings(DB_PATH, config)   # звукові пристрої з панелі
    global recording_archive
    if config.RECORD_AUDIO:
        from shared.recordings import RecordingStore, RecordingArchive
        recording_archive = RecordingArchive(
            RecordingStore(config.RECORDINGS_DIR, retention_days=config.RECORDINGS_RETENTION_DAYS,
                           max_bytes=config.RECORDINGS_MAX_BYTES,
                           low_rms_retention_seconds=config.RECORDINGS_LOW_RMS_RETENTION_SECONDS),
            {key: getattr(config, key) for key in ("MODEL_REPO", "LLM_REPO", "USE_LLM", "LANGUAGE", "SR",
             "GAIN", "MIN_RMS", "NORMALIZE", "VAD_LEVEL", "SILENCE_END_MS", "MIN_SPEECH_MS",
             "MAX_SEGMENT_S", "PREROLL_MS", "SPECTRAL_NOTIFICATION_MODE")}, log)
        log.info("Локальний архів аудіо увімкнено: %s", config.RECORDINGS_DIR)
    telegram_service.archive_root = config.RECORDINGS_DIR
    load_dictionaries()
    recipients = list_recipients(DB_PATH)
    if recipients:
        log.info(
            "Отримувачі сповіщень Telegram: %s",
            ", ".join(f"{r['role']}={r['mode']}" for r in recipients),
        )
    else:
        log.warning(
            "Немає активних користувачів: сповіщення не надсилаються. "
            "Зареєструйте адміністратора: python -m api.bootstrap_admin"
        )

    last_pause_check = float("-inf")
    cached_pause = False

    def pause_requested():
        nonlocal last_pause_check, cached_pause

        now = time.monotonic()

        # Не обращаемся к SQLite на каждом аудиокадре.
        if now - last_pause_check < 0.5:
            return cached_pause

        last_pause_check = now

        try:
            # "pause" — пауза оператора; "listening" — мікрофон закрито, поки в
            # локальній панелі звучить архівний запис (щоб не записати власну луну).
            cached_pause = reception_request(DB_PATH) or False
        except sqlite3.Error:
            log.exception(
                "Не вдалося перевірити команду паузи. "
                "Приймання тимчасово призупиняється."
            )
            cached_pause = True

        return cached_pause

    def report_recognition(kind, value):
        messages = {
            "stuck": (
                "⚠️ <b>Розпізнавання не відповідає</b>\n"
                f"Обробка сегмента триває вже {max(1, round(value / 60))} хв. Аудіо надходить, "
                "але фрази не розпізнаються. Перезапустіть Radio 345."
            ),
            "failing": (
                "⚠️ <b>Розпізнавання не працює</b>\n"
                f"Whisper повертає помилку на {value} сегментах поспіль. "
                "Перевірте журнал radio.log і перезапустіть Radio 345."
            ),
            "recovered": "✅ Розпізнавання знову працює.",
        }
        telegram_service.notify_admin_async(messages[kind])

    def report_suspension(gap_s):
        resumed = datetime.now()
        stopped = resumed - timedelta(seconds=gap_s)
        hours, minutes = divmod(max(1, round(gap_s / 60)), 60)
        duration = f"{hours} год {minutes} хв" if hours else f"{minutes} хв"
        telegram_service.notify_admin_async(
            "⚠️ <b>Розрив циклу під час приймання</b>\n"
            f"З {stopped:%d.%m %H:%M} до {resumed:%d.%m %H:%M} ({duration}).\n"
            "До розриву мікрофон був відкритий. Причину не встановлено: "
            "можливі сон системи або затримка процесу.\n"
            "Перевірте поточний стан приймача в панелі."
        )

    if not shutil.which("ffmpeg"):
        log.warning("Утиліту ffmpeg не знайдено в системі! Конвертація в OGG не працюватиме.")

    telegram_service.start_maintenance()

    global radio_stream
    from shared.audio_devices import configured, input_stream_factory
    from shared.radio_stream import start_radio_stream
    radio_stream = start_radio_stream(config, log)

    supervisor = ApplicationSupervisor(
        transcriber_stop=asr_stop.set,
        transcriber_abandon=asr_abandon.set,
        frames=frames,
        segments=segments,
        on_audio=on_audio,
        transcriber=transcriber,
        telegram_listener=(
            telegram_service.callback_listener if BOT_TOKEN else None
        ),
        sample_rate=SR,
        frame_ms=FRAME_MS,
        frame_len=FRAME_LEN,
        vad_level=config.VAD_LEVEL,
        preroll_ms=PREROLL_MS,
        silence_end_ms=config.SILENCE_END_MS,
        min_speech_ms=config.MIN_SPEECH_MS,
        max_segment_s=config.MAX_SEGMENT_S,
        log=log,
        heartbeat_context=lambda: receiver_heartbeat(
            DB_PATH,
            log,
            state_provider=receiver_runtime.get,
        ),
        pause_checker=pause_requested,
        pause_gate=pause_gate,
        runtime_state=receiver_runtime,
        on_suspension=report_suspension,
        recognition_health=recognition_health,
        on_recognition_problem=report_recognition,
        on_vad_discard=(recording_archive.vad_short
                        if recording_archive and config.RECORDINGS_SAVE_VAD_SHORT else None),
        on_live_audio=radio_stream.live_audio if radio_stream is not None else None,
        input_muted=radio_stream.input_muted if radio_stream is not None else None,
        # Вхід за частиною назви (RADIO_INPUT_DEVICE); при кожному перевідкритті шукається знову.
        stream_factory=input_stream_factory(configured("RADIO_INPUT_DEVICE", config), log),
    )
    # Щоденна перевірена копія бази в .local-backups/ (останні 7).
    backup_stop = start_backup_scheduler(DB_PATH, config.BASE_DIR / ".local-backups", log=log)
    try:
        supervisor.run()
    finally:
        backup_stop.set()
        if radio_stream is not None:
            try:
                radio_stream.stop()
            except Exception:
                log.exception("Не вдалося зупинити потік для iPhone")
        try:
            telegram_service.shutdown(timeout=10.0)
        finally:
            if recording_archive is not None:
                recording_archive.close()
            diagnostics.finish()


if __name__ == "__main__":
    main()
