from pathlib import Path

# ---------- Настройки моделей и обработки ----------

MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
LLM_REPO = "mlx-community/Qwen2.5-3B-Instruct-4bit"

USE_LLM = True
LANGUAGE = "uk"
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = str(BASE_DIR / "events.db")
DICTIONARIES_PATH = BASE_DIR / "dictionaries.txt"
LOG_PATH = BASE_DIR / "radio.log"
DEBUG_AUDIO_DIR = BASE_DIR / "debug_audio"
NOTIFICATION_SOUNDS_DIR = BASE_DIR / ".local-notification-sounds"
FILTER_NOTIFICATION_SOUNDS = True
COOLDOWN_S = 3.0
LOG_CALLSIGNS_ONLY = True

# Чувствительность аудио
GAIN = 1.0
MIN_RMS = 0.02
NORMALIZE = True
DEBUG_RMS = False

# Нарезка фраз и VAD
SR = 16000
FRAME_MS = 30
FRAME_LEN = SR * FRAME_MS // 1000

SILENCE_END_MS = 600
MIN_SPEECH_MS = 500
MAX_SEGMENT_S = 20
PREROLL_MS = 500
VAD_LEVEL = 2

# Фильтрация распознанного текста
BLACKLIST = (
    "редактор субтитров",
    "корректор",
    "субтитры",
    "продолжение следует",
    "спасибо за просмотр",
    "дякую за перегляд",
)

SHORT_GARBAGE = {
    "ах",
    "мамам",
    "па па",
}
