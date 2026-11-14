import os
import sys

# Автоматичний перезапуск через .venv проєкту, якщо запущено іншим Python.
from project_python import select_python

venv_python = select_python()
if os.path.exists(venv_python) and os.path.abspath(sys.executable) != os.path.abspath(venv_python):
    os.execve(venv_python, [venv_python] + sys.argv, os.environ)

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from core.log_security import configure_logging
from core.diagnostics import runtime as diagnostics
import shutil

from core.transcriber_factory import build_transcriber

from core.audio import apply_gain
from core.supervisor import ApplicationSupervisor
from core.pause_gate import PauseGate
from core.cooldown import Cooldown
from core.bounded_queue import DropOldestQueue
from services.receiver_status import (
    receiver_heartbeat,
    ReceiverRuntimeState,
)
from services.receiver_control import read_pause_state
from services.miniapp_management import apply_startup_settings
from dotenv import load_dotenv




from services.event_store import init_db as init_event_db

from services.telegram import TelegramService
from services.dictionary_loader import load_dictionaries as load_sqlite_dictionaries
from services.dictionary_setup import ensure_dictionaries, interactive_console
from services.user_notify_store import list_recipients
from services.db_backup import start_backup_scheduler
from core.instance_lock import acquire_instance_lock
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
recognition_health = RecognitionHealth()
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
        log.warning("Стан аудіопотоку: %s", status)
    frames.put(apply_gain(indata, GAIN))




# ---------- Поток распознавания и LLM ----------
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
    )

    worker.recognizer.warmup()

    if not BOT_TOKEN:
        log.warning("Telegram вимкнено: не задано TELEGRAM_BOT_TOKEN у .env")

    log.info("Слухаю ефір... (Ctrl+C для виходу)")
    worker.run()


def load_dictionaries():
    global callsigns, commands, watch, PROMPT

    callsigns, commands, watch, PROMPT = load_sqlite_dictionaries(
        DB_PATH, log
    )


def main():
    # До створення таблиць: порожні словники пропонуємо імпортувати з dictionaries.txt.
    if not ensure_dictionaries(
        DB_PATH, config.DICTIONARIES_PATH, sys.executable,
        interactive=interactive_console(),
    ):
        raise SystemExit(2)
    # Два приймачі на одному мікрофоні подвоюють події та повідомлення.
    # Блокування тримається до кінця процесу й знімається ОС навіть після аварії.
    global _instance_lock
    _instance_lock = acquire_instance_lock(config.BASE_DIR / ".radio345.lock")
    if _instance_lock is None:
        log.error("Інший приймач Radio 345 уже працює. Зупиніть його й запустіть знову.")
        raise SystemExit(2)
    diagnostics.configure(config.BASE_DIR / "diagnostics.json")
    init_event_db(DB_PATH)
    apply_startup_settings(DB_PATH, config)
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
            cached_pause = read_pause_state(
                DB_PATH
            )["pause_requested"]
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
            "⚠️ <b>Приймання ефіру було перерване</b>\n"
            f"З {stopped:%d.%m %H:%M} до {resumed:%d.%m %H:%M} ({duration}).\n"
            "Ймовірна причина: сон Mac (закрита кришка) або зупинка процесу.\n"
            "Приймання відновлено автоматично."
        )

    if not shutil.which("ffmpeg"):
        log.warning("Утиліту ffmpeg не знайдено в системі! Конвертація в OGG не працюватиме.")

    supervisor = ApplicationSupervisor(
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
    )
    # Щоденна перевірена копія бази в .local-backups/ (останні 7).
    backup_stop = start_backup_scheduler(DB_PATH, config.BASE_DIR / ".local-backups", log=log)
    try:
        supervisor.run()
    finally:
        backup_stop.set()
        telegram_service.shutdown(timeout=10.0)
        diagnostics.flush(force=True)


if __name__ == "__main__":
    main()
