"""Значення за замовчуванням Radio 345 — єдине місце в коді.

Частину з них оператор перекриває в локальній панелі («Розпізнавання → Налаштування»);
у базі зберігається лише змінене відносно цього файла, тож зміна тут діє для всього,
чого в панелі не чіпали. Поточні значення й «типово» видно в панелі.

- Без перезапуску («Обробка без перезапуску»): COOLDOWN_S, LOG_CALLSIGNS_ONLY,
  GARBAGE_*, LOW_CONFIDENCE_*, MIN_RMS, WHISPER_RETRY_WITHOUT_PROMPT,
  WHISPER_PROMPT_DAILY_CODE, SPECTRAL_NOTIFICATION_* (services/runtime_settings.py).
- Після перезапуску приймача («Розпізнавання»): USE_LLM, VAD_LEVEL, SILENCE_END_MS,
  MIN_SPEECH_MS, MAX_SEGMENT_S (services/management/settings.py).
- Решта — лише тут, діє після перезапуску. Секрети й адреси — у .env.
"""
from pathlib import Path

# ---------- Моделі й обробка ----------

# 17.11.2026: повна large-v3 замість turbo. На розмічених записах WER 82,9 % -> 68,6 %,
# мовлення без тексту 33 -> 7 зі 120, числа точно 23 -> 28; на нових записях 79,5 % -> 74,6 %.
# Хибних ключових сповіщень на шумі не більше (1 проти 2). Її вигадки на тиші закривають
# чорний список і правило переліку назв (strip_dictionary_echo).
MODEL_REPO = "mlx-community/whisper-large-v3-mlx"
LLM_REPO = "mlx-community/Qwen2.5-3B-Instruct-4bit"

# Qwen вимкнено 17.11.2026: на 277 записах 0 покращень, +1,6 с на сегмент (docs/lab.md).
USE_LLM = False
LANGUAGE = "uk"
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = str(BASE_DIR / "events.db")
DICTIONARIES_PATH = BASE_DIR / "dictionaries.txt"
LOG_PATH = BASE_DIR / "radio.log"
DEBUG_AUDIO_DIR = BASE_DIR / "debug_audio"
# Приватні знімки кожної сесії: до 14 днів, 100 сесій і 50 МіБ.
DIAGNOSTICS_HISTORY_DAYS = 14
DIAGNOSTICS_HISTORY_MAX_SESSIONS = 100
DIAGNOSTICS_HISTORY_MAX_BYTES = 50 * 1024 * 1024
NOTIFICATION_SOUNDS_DIR = BASE_DIR / ".local-notification-sounds"
# Спектральний фільтр сповіщень: "shadow" — лише оцінює кожен сегмент і показує
# оцінки в локальній панелі, нічого не відкидає; "active" — відкидає кандидатів;
# "off" — вимкнено. Поріг обирається в панелі за розміченими записами «Аудіоархіву».
# 16.11.2026: на розмітці (74 «Сигнали», 112 мовлення) 0,80 / 0,6 с / 0,06 знаходять
# 29 сигналів проти 10 і не зачіпають мовлення: найближче мовлення має ≥2,4 с звуку
# поза зразком. Шум кімнати навколо сигналу давав 0,24–0,54 с і зайву енергію 0,03–0,06.
# 18.11.2026: на розмітці (159 «Сигналів» до 10 с, 252 мовлення) 76 із 102 пропущених
# сигналів відсікав лише ліміт сторонньої енергії (шум ефіру під сигналом, медіана 0,115).
# 0,74 / 0,6 с / 0,15 знаходять 101 сигнал проти 45 і не зачіпають мовлення (0 з 252):
# мовлення відсікає «звук поза збігом» — у нього 2–3 с.
SPECTRAL_NOTIFICATION_MODE = "active"
SPECTRAL_NOTIFICATION_THRESHOLD = 0.74
SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S = 0.6
SPECTRAL_NOTIFICATION_MAX_EXTRA = 0.15
# COOLDOWN_S, LOG_CALLSIGNS_ONLY і GARBAGE_* — початкові значення; змінюються в
# локальній панелі («Налаштування → Обробка без перезапуску»).
COOLDOWN_S = 3.0
LOG_CALLSIGNS_ONLY = True
# Довга фраза — сміття Whisper, якщо слів ≥ MIN_WORDS і (унікальних < RATIO або
# слів більше ніж тривалість × MAX_WORDS_PER_S + EXTRA_WORDS).
GARBAGE_MIN_WORDS = 12
GARBAGE_MIN_UNIQUE_RATIO = 0.25
GARBAGE_MAX_WORDS_PER_S = 5.0
GARBAGE_EXTRA_WORDS = 3
# Короткий текст із низькою впевненістю Whisper — вигадка на звуку без мовлення:
# не більше LOW_CONFIDENCE_MAX_WORDS слів і avg_logprob нижче LOW_CONFIDENCE_LOGPROB
# (на розмічених записах: −57 зі 168 галюцинацій, −1 із 59 записів мовлення).
# Фраза з командою чи позивним із номером не відкидається.
LOW_CONFIDENCE_MAX_WORDS = 2
LOW_CONFIDENCE_LOGPROB = -0.7

# Другий шанс Whisper: якщо з підказкою модель зациклилася («Грозу, Грозу…») і тексту
# не лишилося, сегмент розпізнається ще раз без підказки. 18.11.2026 на 325 еталонах:
# пропущене мовлення 13 → 3, WER 0,617 → 0,613; на 36 записах шуму — 0 хибних подій.
# Повтор лише без підказки: з одними фразами Whisper на шумі повторює їх (хибні команди).
WHISPER_RETRY_WITHOUT_PROMPT = True
# Останній код поточної доби кодів — у підказку Whisper (після фраз, перед назвами).
# 18.11.2026 разом із другим шансом: точних фраз 36 → 39, числа 26 → 30,5 % (475 записів).
WHISPER_PROMPT_DAILY_CODE = True

# Чувствительность аудио
GAIN = 1.0
# Коди доби (services/daily_codes.py) змінюються о цій годині місцевого часу:
# коди «17.11» діють з 16.11 20:00 до 17.11 20:00.
DAILY_CODES_CHANGE_HOUR = 20
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

# Фільтри тексту (чорний список, короткий шум, алиаси, шаблони) живуть у SQLite й
# редагуються в панелі; початкові значення для нової бази — core/default_text_lists.txt.

# Локальний перевірочний корпус: WAV + тексти + метрики, без Telegram.
RECORD_AUDIO = True
RECORDINGS_DIR = BASE_DIR / ".local-recordings"
RECORDINGS_RETENTION_DAYS = 7
RECORDINGS_MAX_BYTES = 5 * 1024 * 1024 * 1024
# Короткі фрагменти, відкинуті VAD (переважно клавіатура й клацання), не зберігаються:
# вони займали майже половину архіву й майже не потрібні для розмітки.
RECORDINGS_SAVE_VAD_SHORT = False
# Frozen sample for offline experiments (python -m tools.asr_lab queue).
# The audio archive UI uses live queues from SQLite, independently of this file.
REVIEW_QUEUE_PATH = BASE_DIR / ".local-experiments" / "review-queue.json"
