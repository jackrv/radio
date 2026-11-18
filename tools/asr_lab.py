"""Відтворювана офлайн-обробка записів «Аудіоархіву».

Без Telegram, мікрофона й робочого циклу приймача. Архів і events.db лише
читаються: правила й словники беруться зі знімка бази, зробленого на початку
запуску. Результати й параметри зберігаються окремо в .local-experiments/.

Приклади:
  .venv/bin/python -m tools.asr_lab inventory
  .venv/bin/python -m tools.asr_lab queue          # зафіксована вибірка для офлайн-порівнянь
  .venv/bin/python -m tools.asr_lab run --name base --variant whisper --status ready,empty_text,blacklist
  .venv/bin/python -m tools.asr_lab run --name no-prompt --variant whisper --prompt none
  .venv/bin/python -m tools.asr_lab run --name band --variant whisper --bandpass on
  .venv/bin/python -m tools.asr_lab report .local-experiments/<запуск>
  .venv/bin/python -m tools.asr_lab compare <запуск A> <запуск B>

Змінюйте один чинник за запуск. Повторний прогін Whisper не є еталоном:
якість рахується лише за розшифровками оператора (reference).
"""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from services.sqlite_connection import read_only

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / ".local-recordings"
OUTPUT = ROOT / ".local-experiments"

# Короткий контекст із підтверджених оператором оборотів (docs/recognition.md).
# Без номерів і без підписів на кшталт «Позивні:» — вони самі потрапляли в галюцинації.
PHRASES_PROMPT = ("Радіообмін. Перевірка зв’язку. На зв’язку. Чую та розбираю добре. "
                  "Прийнято, прийом. Прийняв, прийом. Посилку отримав. "
                  "До роботи приступив. Роботу закінчив. Повторіть, голос не пройшов.")
PROMPTS = ("dictionary", "receiver", "none", "phrases", "combined")

SPLIT_BLOCK_S = 20 * 60
VARIANTS = ("whisper", "rules", "qwen", "pipeline")


# ---------- Дані архіву (лише читання) ----------

def split_of(created):
    """Відкладений набір — кожен третій 20-хвилинний блок ефіру (за хешем).

    Повтори одного виклику й близькі дублікати лежать в одному блоці,
    тож не потрапляють по різні боки поділу.
    """
    block = int(created // SPLIT_BLOCK_S)
    digest = int(hashlib.sha256(f"radio345-split-v1:{block}".encode()).hexdigest(), 16)
    return "holdout" if digest % 3 == 0 else "tune"


def load_records(archive=ARCHIVE, *, statuses=None, labelled_only=False, split="all", limit=None, ids=None):
    with read_only(Path(archive) / "index.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM recordings ORDER BY created").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item["data"])
        item["split"] = split_of(item["created"])
        if statuses and item["status"] not in statuses:
            continue
        if labelled_only and item["label"] is None:
            continue
        if split != "all" and item["split"] != split:
            continue
        if ids is not None and item["id"] not in ids:
            continue
        result.append(item)
    return result[:limit] if limit else result


def read_pcm(archive, record_id):
    path = Path(archive) / f"{record_id}.wav"
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("Непідтримуваний WAV")
        return audio.readframes(audio.getnframes()), audio.getframerate()


# ---------- Метрики ----------

# Слова для WER і сімейств зворотів — спільні з архівом (core/text_metrics.py).
from core.text_metrics import edit_distance, metric_words as words  # noqa: E402
plain_tokens = words
from services.lab_results import FAMILIES, NON_SPEECH, ROOM, SPEECH, UNCLEAR, has_family, load_run, role_tokens, summarize  # noqa: E402,F401


def digit_groups(text):
    """Незалежна перевірка номерів: лише цифри, без парсера числівників застосунку."""
    return re.findall(r"\d+", text or "")


def app_number_groups(text):
    from core.word_learning import number_groups
    return number_groups(text or "")


def score_text(reference, hypothesis):
    ref, hyp = words(reference), words(hypothesis)
    ref_chars, hyp_chars = " ".join(ref), " ".join(hyp)
    return {"word_errors": edit_distance(ref, hyp), "reference_words": len(ref),
            "char_errors": edit_distance(ref_chars, hyp_chars), "reference_chars": len(ref_chars),
            "digits_match": digit_groups(reference) == digit_groups(hypothesis),
            "app_numbers_match": app_number_groups(reference) == app_number_groups(hypothesis),
            "reference_has_digits": bool(digit_groups(reference))}


def bucket(value, edges, unit):
    if value is None:
        return "—"
    for edge in edges:
        if value <= edge:
            return f"≤{edge:g}{unit}"
    return f">{edges[-1]:g}{unit}"


def breakdown(results):
    """WER за тривалістю, гучністю й статусом (лише мовлення з еталоном)."""
    groups = {}
    for r in results:
        if r.get("label") not in SPEECH or r.get("reference") is None:
            continue
        signal = r.get("signal") or {}
        multi = {1: "кілька", 0: "одна"}.get(r.get("multiple_messages"), "не відмічено")
        for key in (("тривалість", bucket(signal.get("duration_s"), (2, 4, 8, 20), " с")),
                    ("гучність", bucket(signal.get("rms_dbfs"), (-38, -34, -30, -24), " dBFS")),
                    ("статус", r.get("status")), ("передач у записі", multi)):
            g = groups.setdefault(key, {"word_errors": 0, "reference_words": 0, "records": 0})
            g["word_errors"] += r["score"]["word_errors"]
            g["reference_words"] += r["score"]["reference_words"]
            g["records"] += 1
    return {f"{k[0]}: {k[1]}": {**v, "wer": v["word_errors"] / v["reference_words"] if v["reference_words"] else None}
            for k, v in sorted(groups.items())}


# ---------- Конвеєр ----------

def bandpass(pcm, sample_rate, low=300.0, high=3400.0, ramp=100.0):
    """Смуга мовлення рації перед Whisper: частотна маска з плавними краями, без зсуву фази.

    Гул кімнати нижче й удари клавіш та шипіння вище смуги послаблюються.
    Обробляє весь запис одразу (офлайн); для живого приймання потрібен потоковий фільтр.
    """
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if not x.size:
        return pcm
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / sample_rate)
    rise = np.clip((freqs - (low - ramp)) / ramp, 0.0, 1.0)
    fall = np.clip(((high + ramp) - freqs) / ramp, 0.0, 1.0)
    mask = 0.5 - 0.5 * np.cos(np.pi * np.minimum(rise, fall))
    y = np.fft.irfft(spectrum * mask, len(x))
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


def snapshot_database(target):
    """Знімок events.db для відтворюваності; робоча база не змінюється."""
    import config
    with read_only(config.DB_PATH) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    os.chmod(target, 0o600)
    # Таблиці правил у копії (стара база могла їх не мати); робоча база не змінюється.
    from services.word_store import init_word_db
    init_word_db(target)
    return target


def receiver_prompt(db_path, dictionary_prompt, phrases=None, extra=()):
    """Підказка, як у приймачі: фрази списку «Підказка Whisper» + назви словників.

    ``phrases`` — інший перелік фраз (файл --whisper-phrases); ``extra`` — додаткові
    фрази перед назвами (коди доби запису для --daily-codes).
    """
    from services.text_lists import compose_prompt, load_active
    if phrases is None:
        with read_only(db_path) as db:
            _, values = load_active(db)
        phrases = values.get("whisper_prompt", ())
    return compose_prompt((*phrases, *extra), dictionary_prompt)


def record_codes(db_path, created, mode="append"):
    """Коди доби запису (доба за межею 20:00) зі знімка бази.

    append — постійні й усі коди доби; latest — лише останній доданий код цієї доби.
    """
    from services import daily_codes
    day = daily_codes.local_day(created)
    with read_only(db_path) as db:
        try:
            return daily_codes.read_day(db, day)[-1:] if mode == "latest" else daily_codes.read(db, day)
        except sqlite3.Error:
            return []


def dictionaries_from(db_path):
    from services.dictionary_loader import build_prompt, dictionaries_from_lines, read_dictionary_lines
    with read_only(db_path) as db:
        lines = read_dictionary_lines(db)
        try:
            from services.text_lists import load_active
            from core.callsigns import configure_aliases
            _, values = load_active(db)
            configure_aliases(values["aliases"], values["callsign_aliases"])
            blacklist, short_garbage = values["blacklist"], values["short_garbage"]
        except sqlite3.Error:
            from core.default_text_lists import enabled
            blacklist, short_garbage = list(enabled("blacklist")), list(enabled("short_garbage"))
    callsigns, commands, watch = dictionaries_from_lines(lines)
    return (callsigns, commands, watch), build_prompt(callsigns, commands, watch), blacklist, short_garbage


def prompt_token_count(prompt, language):
    """Кількість токенів підказки токенізатором Whisper (зберігається ≤223 останніх)."""
    if not prompt:
        return 0
    try:
        from mlx_whisper.tokenizer import get_tokenizer
        tokenizer = get_tokenizer(multilingual=True, num_languages=100, language=language or "uk", task="transcribe")
        return len(tokenizer.encode(" " + prompt.strip()))
    except Exception:
        return None


def dictionary_fingerprint_of(db_path):
    from services.dictionary_loader import dictionary_fingerprint
    try:
        with read_only(db_path) as db:
            return dictionary_fingerprint(db)
    except sqlite3.Error:
        return None


def runtime_versions(model_repo):
    """Версії рушія й ревізія локальних ваг (каталог snapshot HF — це хеш коміту)."""
    from importlib import metadata
    versions = {"python": sys.version.split()[0]}
    for package in ("mlx-whisper", "mlx", "numpy"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    try:
        from huggingface_hub import try_to_load_from_cache
        path = try_to_load_from_cache(model_repo, "config.json")
        versions["model_revision"] = Path(path).parent.name if isinstance(path, str) else None
    except Exception:
        versions["model_revision"] = None
    return versions


def parse_whisper_options(items):
    """«ключ=значення» для mlx_whisper.transcribe: true/false, числа, інакше рядок."""
    options = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--whisper-option: очікується «ключ=значення», отримано «{item}»")
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            value = lowered == "true"
        else:
            try:
                value = float(raw) if any(c in raw for c in ".eE") else int(raw)
            except ValueError:
                value = raw
        options[key.strip()] = value
    return options


class Lab:
    def __init__(self, args, run_dir):
        import config
        self.args = args
        self.log = logging.getLogger("asr_lab")
        self.db_path = snapshot_database(run_dir / "events-snapshot.db")
        self.dictionaries, dictionary_prompt, self.blacklist, self.short_garbage = dictionaries_from(self.db_path)
        if getattr(args, "names", None):
            # Інший перелік назв у підказці (напр. лише ті, що звучать в ефірі): рядок — назва.
            names = [l.strip() for l in Path(args.names).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            dictionary_prompt = ", ".join(sorted(set(names)))
        phrases = Path(args.prompt_file).read_text(encoding="utf-8").strip() if args.prompt_file else PHRASES_PROMPT
        self.whisper_phrases = None
        if getattr(args, "whisper_phrases", None):
            self.whisper_phrases = [line.strip() for line in Path(args.whisper_phrases).read_text(encoding="utf-8").splitlines()
                                    if line.strip() and not line.startswith("#")]
        if args.prompt == "receiver":
            self.prompt = receiver_prompt(self.db_path, dictionary_prompt, self.whisper_phrases)
        else:
            self.prompt = {"dictionary": dictionary_prompt, "none": "", "phrases": phrases,
                           "combined": f"{phrases} {dictionary_prompt}".strip()}[args.prompt]
        self.dictionary_prompt = dictionary_prompt
        self.language = None if args.language == "auto" else args.language
        self.model = args.model or config.MODEL_REPO
        self.sample_rate = config.SR
        from core.recognition import WhisperRecognizer
        self.temperature = tuple(float(t) for t in getattr(args, "temperature", "0,0.2,0.4").split(","))
        self.whisper_options = parse_whisper_options(getattr(args, "whisper_option", None) or [])
        self.recognizer = WhisperRecognizer(self.model, self.language, self.sample_rate, self.prompt, self.log,
                                            temperature=self.temperature, options=self.whisper_options)
        from services.runtime_settings import _read
        with read_only(self.db_path) as db:
            self.runtime_values, self.runtime_revision = _read(db)
        self.garbage_limits = {
            "min_words": self.runtime_values["GARBAGE_MIN_WORDS"],
            "min_unique_ratio": self.runtime_values["GARBAGE_MIN_UNIQUE_RATIO"],
            "max_words_per_s": self.runtime_values["GARBAGE_MAX_WORDS_PER_S"],
            "extra_words": self.runtime_values["GARBAGE_EXTRA_WORDS"],
        }
        self.llm = None
        if args.variant == "qwen" and getattr(args, "llm", "on") == "off":
            raise SystemExit("--llm off має сенс лише для --variant pipeline")
        if args.variant in ("qwen", "pipeline") and getattr(args, "llm", "on") == "on":
            from core.correction import build_llm_corrector
            callsigns, commands, _ = self.dictionaries
            self.llm = build_llm_corrector(True, config.LLM_REPO, callsigns, commands, self.log)

    def params(self):
        import config
        return {"variant": self.args.variant, "pipeline_scope": "text-after-whisper-v2",
                "runtime_settings": self.runtime_values, "runtime_settings_revision": self.runtime_revision,
                "not_replayed": ["notification_spectral", "cooldown", "recipient_modes", "telegram_delivery"],
                "llm": getattr(self.args, "llm", "on") if self.args.variant in ("qwen", "pipeline") else None,
                "llm_repo": config.LLM_REPO if self.llm else None, "temperature": list(self.temperature), "model": self.model, "language": self.language or "auto",
                "prompt": self.args.prompt, "prompt_chars": len(self.prompt), "prompt_text": self.prompt,
                "daily_codes": getattr(self.args, "daily_codes", "off"), "whisper_phrases": self.whisper_phrases,
                "retry": getattr(self.args, "retry", "off"), "names_file": getattr(self.args, "names", None),
                "prompt_tokens": prompt_token_count(self.prompt, self.language),
                "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
                "dictionary_prompt_sha256": hashlib.sha256(self.dictionary_prompt.encode()).hexdigest(),
                "dictionary_fingerprint": dictionary_fingerprint_of(self.db_path),
                "versions": runtime_versions(self.model),
                "normalize": self.args.normalize == "on",
                "min_rms_gate": self.args.min_rms, "llm_repo": config.LLM_REPO if self.llm else None,
                "whisper_options": {"temperature": list(self.temperature), "condition_on_previous_text": False,
                                    "no_speech_threshold": 0.6, "logprob_threshold": -1.0,
                                    "compression_ratio_threshold": 2.4, **self.whisper_options},
                "seed": self.args.seed, "resegment": self.args.resegment, "gate_db": self.args.gate_db,
                "bandpass": [self.args.band_low, self.args.band_high] if self.args.bandpass == "on" else None,
                "split": self.args.split, "statuses": self.args.status, "labelled_only": self.args.labelled_only}

    def apply_rules(self, text):
        from services.word_rule_engine import apply_verified_word_rules
        revised = apply_verified_word_rules(text, self.db_path, self.dictionaries)
        return revised, revised != text

    def resegment(self, pcm):
        """Повторне різання запису сегментатором VAD із додатковою умовою гучності.

        Кадр вважається мовленням, лише якщо WebRTC VAD так вважає І він гучніший
        за фон запису (10-й перцентиль кадрів) на --gate-db. Так пауза, заповнена
        шумом ефіру, закриває сегмент через SILENCE_END_MS, як справжня тиша.
        """
        import config
        from core.vad import VADSegmenter
        frame = config.FRAME_LEN * 2
        frames = [pcm[i:i + frame] for i in range(0, len(pcm) - frame + 1, frame)]
        if not frames:
            return [pcm]
        levels = [20 * math.log10(math.sqrt(float(np.mean(np.frombuffer(f, "<i2").astype(np.float64) ** 2)) / 32768 ** 2) + 1e-12)
                  for f in frames]
        # Фон — за кадрами з реальним сигналом: цифрові нулі дали б −240 дБ.
        audible = [v for v in levels if v > -100] or levels
        floor = float(np.percentile(audible, 10))
        level_of = dict(zip(map(id, frames), levels))
        # Невідомі кадри (додана в кінці тиша) — не мовлення.
        gate = lambda f: level_of.get(id(f), -240.0) > floor + self.args.gate_db
        segmenter = VADSegmenter(config.SR, config.FRAME_MS, config.VAD_LEVEL, config.PREROLL_MS,
                                 config.SILENCE_END_MS, config.MIN_SPEECH_MS, config.MAX_SEGMENT_S, speech_gate=gate)
        pieces = []
        silence = b"\0" * frame
        for f in frames + [silence] * (config.SILENCE_END_MS // config.FRAME_MS + 1):
            piece = segmenter.process(f)
            if piece:
                pieces.append(piece)
        return pieces

    def process(self, pcm):
        import config
        from core.audio import prepare_audio
        from core.analyzer import fmt, is_low_confidence_short, looks_like_garbage, split_roles, strip_dictionary_echo
        from core.pipeline import build_text_pipeline
        from core.analyzer import correction_is_safe
        from services.text_lists import blacklist_key
        timing = {}
        started = time.perf_counter()
        audio, rms = prepare_audio(pcm, 0.0, self.args.normalize == "on")
        # Поріг RMS — як у приймачі: за вихідним звуком, до будь-яких фільтрів.
        gated = self.args.min_rms is not None and rms < self.args.min_rms
        result = {"rms": rms, "gated": gated}
        if gated or audio.size == 0:
            result.update(hypothesis="", raw_text="", stage="rms_gate")
            timing["total_s"] = time.perf_counter() - started
            return result, timing
        if self.args.bandpass == "on":
            pcm = bandpass(pcm, self.sample_rate, self.args.band_low, self.args.band_high)
            audio, _ = prepare_audio(pcm, 0.0, self.args.normalize == "on")
        t = time.perf_counter()
        try:
            # Відкат на температуру 0,2/0,4 — випадкова вибірка. Фіксуємо зерно,
            # щоб однакові варіанти давали однаковий текст між запусками.
            import mlx.core as mx
            mx.random.seed(self.args.seed)
        except ImportError:
            pass
        if self.args.resegment == "energy":
            pieces = self.resegment(pcm)
            texts, segments_meta = [], []
            for piece in pieces:
                piece_audio, _ = prepare_audio(piece, 0.0, self.args.normalize == "on")
                out = self.recognizer.transcribe(piece_audio)
                texts.append(self.recognizer.extract_text(out))
                segments_meta += [{k: s.get(k) for k in ("avg_logprob", "no_speech_prob", "compression_ratio",
                                                         "temperature", "start", "end", "text")} for s in out.get("segments", [])]
            raw = " ".join(t for t in texts if t).strip()
            result["segments"] = len(pieces)
            result["segment_durations_s"] = [round(len(piece) / (2 * self.sample_rate), 2) for piece in pieces]
            result["segment_texts"] = texts
            result["whisper_segments"] = segments_meta
        else:
            whisper = self.recognizer.transcribe(audio)
            raw = self.recognizer.extract_text(whisper)
            # Як у приймачі: зациклився з підказкою й тексту немає — ще раз без підказки.
            from core.recognition import is_looped
            if getattr(self.args, "retry", "off") == "on" and not raw and self.recognizer.prompt and is_looped(whisper):
                again = self.recognizer.transcribe(audio, prompt="")
                result["whisper_retry"] = bool(self.recognizer.extract_text(again))
                if result["whisper_retry"]:
                    whisper, raw = again, self.recognizer.extract_text(again)
            result["whisper_segments"] = [{k: s.get(k) for k in ("avg_logprob", "no_speech_prob", "compression_ratio",
                                                                 "temperature", "start", "end", "text")}
                                          for s in whisper.get("segments", [])]
        timing["whisper_s"] = time.perf_counter() - t
        result["raw_text"] = raw
        text, stage = raw, "whisper"
        if self.args.variant == "rules":
            text, _ = self.apply_rules(raw)
            stage = "rules"
        elif self.args.variant == "qwen":
            t = time.perf_counter()
            pipeline = build_text_pipeline(self.llm, lambda value: (value, False), self.dictionaries,
                                           correction_is_safe, self.log)
            text = pipeline(raw) if raw else raw
            timing["qwen_s"] = time.perf_counter() - t
            stage = "qwen"
        elif self.args.variant == "pipeline":
            duration = len(pcm) / (2 * self.sample_rate)
            if not raw:
                stage = "empty_text"
            elif looks_like_garbage(raw, duration, **self.garbage_limits):
                text, stage = "", "garbage"
            elif blacklist_key(raw) in {blacklist_key(v) for v in self.blacklist}:
                text, stage = "", "blacklist"
            else:
                # Перелік назв словника з підказки — як у приймачі (core/transcriber.py).
                cleaned, echo = strip_dictionary_echo(raw, self.dictionaries)
                if echo:
                    result["dictionary_echo"] = True
                if echo and not cleaned:
                    text, stage = "", "dictionary_echo"
                elif echo and blacklist_key(cleaned) in {blacklist_key(v) for v in self.blacklist}:
                    text, stage = "", "blacklist"
                elif is_low_confidence_short(cleaned, result["whisper_segments"], self.dictionaries[0],
                                             self.dictionaries[1], max_words=config.LOW_CONFIDENCE_MAX_WORDS,
                                             logprob=config.LOW_CONFIDENCE_LOGPROB):
                    text, stage = "", "low_confidence"
                else:
                    t = time.perf_counter()
                    pipeline = build_text_pipeline(self.llm, self.apply_rules, self.dictionaries, correction_is_safe, self.log)
                    text = pipeline(cleaned)
                    timing["correction_s"] = time.perf_counter() - t
                    from core.text_analysis import analyze_text
                    analysis = analyze_text(text, *self.dictionaries, self.short_garbage, self.log)
                    text = "" if analysis.rejected else analysis.text
                    stage = analysis.rejected or "pipeline"
        result["hypothesis"] = text
        result["stage"] = stage
        callsigns = self.dictionaries[0]
        to_found, from_found = split_roles(text, callsigns)
        result["hypothesis_recipient"], result["hypothesis_sender"] = fmt(to_found), fmt(from_found)
        timing["total_s"] = time.perf_counter() - started
        return result, timing


def roles_from_reference(reference, callsigns):
    """Логіка «кому / від кого» на правильному тексті: окремо від помилок слуху моделі."""
    from core.analyzer import fmt, split_roles
    to_found, from_found = split_roles(reference, callsigns)
    return fmt(to_found), fmt(from_found)


def keep_awake():
    """Поки йде прогін, Mac не засинає через бездіяльність (без приймача мікрофон закритий).

    18.11.2026 нічний прогін зупинився: при зупиненому приймачі Mac заснув за кілька хвилин.
    caffeinate сам завершується разом із цим процесом.
    """
    import os
    import shutil
    import subprocess
    if shutil.which("caffeinate"):
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def run(args):
    keep_awake()
    if getattr(args, "daily_codes", None) is None:
        # Як у приймачі: код доби в підказці, якщо його ввімкнено в config.py.
        import config
        args.daily_codes = "latest" if getattr(config, "WHISPER_PROMPT_DAILY_CODE", False) else "off"
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUTPUT / f"{started}_{re.sub(r'[^A-Za-z0-9_-]+', '-', args.name)}"
    run_dir.mkdir(parents=True, mode=0o700)
    OUTPUT.chmod(0o700)
    statuses = set(args.status.split(",")) if args.status else None
    ids = set(Path(args.ids).read_text().split()) if args.ids else None
    records = load_records(args.archive, statuses=statuses, labelled_only=args.labelled_only,
                           split=args.split, limit=args.limit, ids=ids)
    lab = Lab(args, run_dir)
    params = {**lab.params(), "records": len(records), "archive": str(Path(args.archive).resolve()),
              "started_utc": started, "git_head": os.popen(f"git -C {ROOT} rev-parse HEAD").read().strip()}
    (run_dir / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2))
    results = []
    with open(run_dir / "results.jsonl", "w", encoding="utf-8") as out:
        for index, record in enumerate(records, 1):
            try:
                pcm, sr = read_pcm(args.archive, record["id"])
                if sr != lab.sample_rate:
                    raise ValueError("Інша частота дискретизації")
                if getattr(args, "daily_codes", "off") != "off" and args.prompt == "receiver":
                    # Як могло б бути в приймачі: коди доби саме цього запису — у підказці.
                    lab.recognizer.prompt = receiver_prompt(lab.db_path, lab.dictionary_prompt, lab.whisper_phrases,
                                                            record_codes(lab.db_path, record["created"], args.daily_codes))
                output, timing = lab.process(pcm)
            except Exception as exc:  # один зіпсований запис не зупиняє експеримент
                output, timing = {"error": type(exc).__name__}, {}
            try:
                wav_sha = hashlib.sha256((Path(args.archive) / f"{record['id']}.wav").read_bytes()).hexdigest()
            except OSError:
                wav_sha = None
            item = {"id": record["id"], "created": record["created"], "split": record["split"],
                    "wav_sha256": wav_sha, "session": record["data"].get("session"),
                    "status": record["status"], "label": record["label"], "reference": record["reference"],
                    "reference_recipient": record["reference_recipient"], "reference_sender": record["reference_sender"],
                    "multiple_messages": record.get("multiple_messages"),
                    "signal": record["data"].get("signal"), "original_raw_text": record["data"].get("raw_text"),
                    **output, "timing": timing}
            if record["reference"] is not None:
                item["score"] = score_text(record["reference"], output.get("hypothesis", ""))
                item["reference_recipient_parsed"], item["reference_sender_parsed"] = roles_from_reference(
                    record["reference"], lab.dictionaries[0])
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
            out.flush()
            results.append(item)
            print(f"\r{index}/{len(records)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    write_report(run_dir, results)
    print(run_dir)


metrics_for = summarize


def write_report(run_dir, results):
    report = {"summary": metrics_for(results), "breakdown": breakdown(results),
              "by_split": {s: metrics_for([r for r in results if r.get("split") == s]) for s in ("tune", "holdout")}}
    (Path(run_dir) / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def compare(a_dir, b_dir, show=10):
    params_a, a = load_run(a_dir)
    params_b, b = load_run(b_dir)
    common = {r["id"] for r in a} & {r["id"] for r in b}
    a = [r for r in a if r["id"] in common]
    b = [r for r in b if r["id"] in common]
    sa, sb = metrics_for(a), metrics_for(b)
    print(f"Спільних записів: {len(common)}\n")
    for key in sorted(set(sa) | set(sb)):
        print(f"  {key:40s} {sa.get(key)!s:>22} → {sb.get(key)!s}")
    by_id = {r["id"]: r for r in b}
    changes = []
    for r in a:
        other = by_id[r["id"]]
        if "score" in r and "score" in other and r["score"]["word_errors"] != other["score"]["word_errors"]:
            changes.append((other["score"]["word_errors"] - r["score"]["word_errors"], r, other))
    print(f"\nПокращень: {sum(d < 0 for d, _, _ in changes)}, погіршень: {sum(d > 0 for d, _, _ in changes)}")
    for delta, r, other in sorted(changes, key=lambda c: c[0])[:show] + sorted(changes, key=lambda c: -c[0])[:show]:
        print(f"  {delta:+d} слів | {r['id'][:8]} | еталон: {r['reference']!r}\n        A: {r.get('hypothesis')!r}\n        B: {other.get('hypothesis')!r}")


def inventory(args):
    records = load_records(args.archive)
    from collections import Counter
    statuses = Counter(r["status"] for r in records)
    labels = Counter(r["label"] for r in records)
    print(f"Записів: {len(records)} | статуси: {dict(statuses)}")
    print(f"Мітки: {dict(labels)} | еталонів: {sum(r['reference'] is not None for r in records)}")
    print(f"Ролі: кому {sum(r['reference_recipient'] is not None for r in records)}, "
          f"від {sum(r['reference_sender'] is not None for r in records)}")
    print(f"Поділ: {dict(Counter(r['split'] for r in records))} "
          f"(розмічених: {dict(Counter(r['split'] for r in records if r['label'] is not None))})")


QUEUE_GROUPS = (
    ("A", "Усе, що бачив Whisper (ready, empty_text, blacklist)"),
    ("B", "Відсічені за RMS поруч із порогом (−36…−34 dBFS)"),
    ("C", "Відсічені за RMS, нижче −36 dBFS"),
    ("D", "Відкинуті VAD, мовлення 300–490 мс"),
)


def build_queue(records, *, seed=345, sizes=(None, 15, 10, 20)):
    """Стратифікована вибірка нерозмічених записів; детермінована за зерном."""
    import random
    rng = random.Random(seed)
    pending = [r for r in records if r["label"] is None]
    rms = lambda r: (r["data"].get("signal") or {}).get("rms_dbfs") or -120
    selectors = (
        lambda r: r["status"] in ("ready", "empty_text", "blacklist", "low_confidence"),
        lambda r: r["status"] == "low_rms" and -36 <= rms(r) < -34,
        lambda r: r["status"] == "low_rms" and rms(r) < -36,
        lambda r: r["status"] == "vad_short" and (r["data"].get("speech_ms") or 0) >= 300,
    )
    groups = []
    for (key, title), select, size in zip(QUEUE_GROUPS, selectors, sizes):
        ids = sorted(r["id"] for r in pending if select(r))
        if size is not None:
            rng.shuffle(ids)
            ids = sorted(ids[:size])
        groups.append({"key": key, "title": title, "ids": ids})
    return {"version": 1, "created": time.time(), "seed": seed, "groups": groups}


def write_queue(args):
    import config
    queue = build_queue(load_records(args.archive), seed=args.seed)
    path = Path(args.output or config.REVIEW_QUEUE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    for group in queue["groups"]:
        print(f"{group['key']}: {len(group['ids'])} — {group['title']}")
    print(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", default=str(ARCHIVE))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    qp = sub.add_parser("queue", help="зафіксувати вибірку записів для офлайн-порівнянь")
    qp.add_argument("--seed", type=int, default=345)
    qp.add_argument("--output", help="типово config.REVIEW_QUEUE_PATH")
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--variant", choices=VARIANTS, default="whisper")
    r.add_argument("--status", help="статуси через кому (типово — усі)")
    r.add_argument("--split", choices=("all", "tune", "holdout"), default="all")
    r.add_argument("--labelled-only", action="store_true")
    r.add_argument("--limit", type=int)
    r.add_argument("--ids", help="файл з ідентифікаторами записів")
    r.add_argument("--language", default="uk", help="uk, ru або auto")
    r.add_argument("--prompt", choices=PROMPTS, default="dictionary",
                   help="dictionary — лише назви словників; receiver — як у приймачі (список «Підказка Whisper» "
                        "+ назви); phrases — лише звороти; combined — звороти + словник")
    r.add_argument("--prompt-file", help="власний текст зворотів для phrases/combined (без номерів)")
    r.add_argument("--whisper-phrases", help="файл фраз «Підказки Whisper» замість списку з бази (для --prompt receiver)")
    r.add_argument("--daily-codes", choices=("off", "append", "latest"), default=None,
                   help="append: додати в підказку постійні й коди доби кожного запису; latest — лише останній "
                        "код доби запису (лише --prompt receiver)")
    r.add_argument("--retry", choices=("off", "on"), default="off",
                   help="on: другий шанс без підказки, коли Whisper зациклився й тексту немає (як у приймачі)")
    r.add_argument("--names", help="файл назв для підказки замість усіх назв словників (рядок — назва)")
    r.add_argument("--normalize", choices=("on", "off"), default="on")
    r.add_argument("--temperature", default="0,0.2,0.4",
                   help="температури Whisper через кому; «0» — без повторного розпізнавання")
    r.add_argument("--llm", choices=("on", "off"), default="on",
                   help="off: pipeline без Qwen (решта обробки як у приймачі)")
    r.add_argument("--min-rms", type=float, default=None, help="поріг RMS (типово — без відсікання)")
    r.add_argument("--model", help="інша модель Whisper (типово — з config.py)")
    r.add_argument("--seed", type=int, default=345, help="зерно для випадкового відкату температури")
    r.add_argument("--resegment", choices=("none", "energy"), default="none",
                   help="energy — повторно різати запис VAD із умовою гучності над фоном")
    r.add_argument("--gate-db", type=float, default=12.0, help="поріг над фоном для --resegment energy, дБ")
    r.add_argument("--bandpass", choices=("off", "on"), default="off", help="смуга мовлення перед Whisper")
    r.add_argument("--band-low", type=float, default=300.0, help="нижня межа смуги, Гц")
    r.add_argument("--band-high", type=float, default=3400.0, help="верхня межа смуги, Гц")
    r.add_argument("--whisper-option", action="append", metavar="КЛЮЧ=ЗНАЧЕННЯ",
                   help="додатковий параметр mlx_whisper.transcribe, напр. hallucination_silence_threshold=2 "
                        "(можна кілька разів)")
    rep = sub.add_parser("report")
    rep.add_argument("run_dir")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.command == "inventory":
        inventory(args)
    elif args.command == "queue":
        write_queue(args)
    elif args.command == "run":
        run(args)
    elif args.command == "report":
        _, results = load_run(args.run_dir)
        print(json.dumps(write_report(args.run_dir, results), ensure_ascii=False, indent=2))
    else:
        compare(args.a, args.b)


if __name__ == "__main__":
    main()
