import os
import sys

# Автоматический перезапуск через локальный venv, если скрипт запущен системным Python
venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python")
if os.path.exists(venv_python) and os.path.abspath(sys.executable) != os.path.abspath(venv_python):
    os.execve(venv_python, [venv_python] + sys.argv, os.environ)

import logging
import sqlite3
import time
from core.log_security import configure_logging
import queue
import shutil

from core.transcriber_factory import build_transcriber

from core.audio import apply_gain
from core.supervisor import ApplicationSupervisor
from core.pause_gate import PauseGate
from core.cooldown import Cooldown
from core.bounded_queue import DropOldestQueue
from services.bot_state import BotState
from services.receiver_status import (
    receiver_heartbeat,
    ReceiverRuntimeState,
)
from services.receiver_control import read_pause_state
from dotenv import load_dotenv




from services.event_store import init_db as init_event_db

from services.telegram import TelegramService
from services.dictionary_loader import load_dictionaries as load_sqlite_dictionaries

import config

from config import (
    DB_PATH,
    COOLDOWN_S,
    GAIN,
    SR,
    FRAME_MS,
    FRAME_LEN,
    SILENCE_END_MS,
    MIN_SPEECH_MS,
    MAX_SEGMENT_S,
    PREROLL_MS,
    VAD_LEVEL,
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
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---------- Настройки импортированы из config.py ----------

callsigns = None
commands = None
watch = None
PROMPT = ""

frames = DropOldestQueue(100)
segments = DropOldestQueue(5)
cooldown = Cooldown(COOLDOWN_S)

# Состояние бота и режимы уведомлений (потокобезопасный доступ через lock)
bot_state = BotState()
pause_gate = PauseGate()
receiver_runtime = ReceiverRuntimeState()

telegram_service = TelegramService(
    bot_token=BOT_TOKEN,
    chat_id=CHAT_ID,
    sample_rate=SR,
    db_path=DB_PATH,
    log=log,
    state_lock=bot_state.lock,
    waiting_for_correction=bot_state.waiting_for_correction,
    get_notify_mode=bot_state.get_notify_mode,
    set_notify_mode=bot_state.set_notify_mode,
)

# ---------- База данных (потокобезопасно) ----------
# ---------- Утилиты ----------
def on_audio(indata, frame_count, time_info, status):
    if status:
        log.warning("Статус аудиопотока: %s", status)
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
        bot_state=bot_state,
        log=log,
        pause_gate=pause_gate,
    )

    worker.recognizer.warmup()

    if not (BOT_TOKEN and CHAT_ID):
        log.warning(
            "Telegram выключен: не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env"
        )

    log.info("Слушаю эфир... (Ctrl+C для выхода)")
    worker.run()


def load_dictionaries():
    global callsigns, commands, watch, PROMPT

    callsigns, commands, watch, PROMPT = load_sqlite_dictionaries(
        DB_PATH, log
    )


def main():
    init_event_db(DB_PATH)
    load_dictionaries()
    bot_state.enable_persistence(DB_PATH)
    log.info(
        "Режим Telegram восстановлен: %s",
        bot_state.get_notify_mode(),
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
                "Не удалось проверить команду паузы. "
                "Приём временно приостанавливается."
            )
            cached_pause = True

        return cached_pause

    if not shutil.which("ffmpeg"):
        log.warning("Утилита ffmpeg не найдена в системе! Конвертация в OGG работать не будет.")

    supervisor = ApplicationSupervisor(
        frames=frames,
        segments=segments,
        on_audio=on_audio,
        transcriber=transcriber,
        telegram_listener=(
            telegram_service.callback_listener if BOT_TOKEN and CHAT_ID else None
        ),
        sample_rate=SR,
        frame_ms=FRAME_MS,
        frame_len=FRAME_LEN,
        vad_level=VAD_LEVEL,
        preroll_ms=PREROLL_MS,
        silence_end_ms=SILENCE_END_MS,
        min_speech_ms=MIN_SPEECH_MS,
        max_segment_s=MAX_SEGMENT_S,
        log=log,
        heartbeat_context=lambda: receiver_heartbeat(
            DB_PATH,
            log,
            state_provider=receiver_runtime.get,
        ),
        pause_checker=pause_requested,
        pause_gate=pause_gate,
        runtime_state=receiver_runtime,
    )
    try:
        supervisor.run()
    finally:
        telegram_service.shutdown(timeout=10.0)


if __name__ == "__main__":
    main()